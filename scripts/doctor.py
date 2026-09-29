#!/usr/bin/env python3
"""QuantDesk doctor.

Runs every check that cannot be run without a real machine, real network and
real containers, then prints one report. Run this on your Mac and paste the
whole output back — it tells me exactly what broke and where, so a round of
fixes takes one message instead of five.

    python3 scripts/doctor.py              # everything
    python3 scripts/doctor.py --no-network # skip NSE and Yahoo
    python3 scripts/doctor.py --api        # also hit a running backend

Safe to run any time. It never places an order and never changes the
schema. One check writes: the candle-upsert check inserts and then deletes
twenty `__DOCTOR__` bars, under the writer lease, so it cannot run during
a migration (Pass 2E-A.1).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "backend", Path("/srv")):
    if candidate.exists():
        sys.path.insert(0, str(candidate))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn ", SKIP: " skip "}[status]
    print(f"[{mark}] {name}" + (f"\n         {detail}" if detail else ""), flush=True)


def check(name: str):
    """Decorator: run a check, catch anything, record the outcome."""
    def wrap(fn):
        try:
            detail = fn()
            record(name, PASS, detail or "")
        except SkipCheck as exc:
            record(name, SKIP, str(exc))
        except WarnCheck as exc:
            record(name, WARN, str(exc))
        except Exception as exc:
            line = traceback.extract_tb(exc.__traceback__)[-1]
            where = f"{line.filename.split('/')[-1]}:{line.lineno}"
            record(name, FAIL, f"{type(exc).__name__}: {exc}  ({where})")
        return fn
    return wrap


class SkipCheck(Exception):
    pass


class WarnCheck(Exception):
    pass


# ---------------------------------------------------------------------------
def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 56 - len(title)), flush=True)


def run_environment() -> None:
    section("environment")

    @check("python version")
    def _():
        v = sys.version_info
        if v < (3, 11):
            raise WarnCheck(f"Python {v.major}.{v.minor}; the backend targets 3.12")
        return f"Python {v.major}.{v.minor}.{v.micro} on {platform.machine()}"

    # `curl_cffi`, not `yfinance`. The free broker talks to Yahoo's chart
    # endpoint over HTTP itself; yfinance is not a dependency and is not
    # installed. Requiring it here failed the doctor for a package the
    # application deliberately does not carry, which trains you to read a
    # red line as normal — the one habit a diagnostic must never teach.
    for module in ("pandas", "numpy", "fastapi", "sqlalchemy", "redis",
                   "httpx", "apscheduler", "curl_cffi"):
        @check(f"import {module}")
        def _(m=module):
            mod = __import__(m)
            return getattr(mod, "__version__", "version unknown")


def run_analytics() -> None:
    section("analytics (no network needed)")

    @check("mock broker generates candles")
    def _():
        from app.brokers.mock import MockBroker
        df = MockBroker().candles(days=5)
        if df.empty:
            raise RuntimeError("mock broker returned an empty frame")
        return f"{len(df)} candles, {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}"

    @check("indicators compute without NaN leakage")
    def _():
        from app.analytics import indicators
        from app.brokers.mock import MockBroker
        df = indicators.enrich(MockBroker().candles(days=5))
        tail = df.tail(50)
        bad = [c for c in ("ema20", "atr14", "vwap", "rvol") if tail[c].isna().any()]
        if bad:
            raise RuntimeError(f"NaN in the last 50 bars of: {bad}")
        return f"vwap={df['vwap'].iloc[-1]:.2f} atr14={df['atr14'].iloc[-1]:.2f}"

    @check("signal engine produces a decision")
    def _():
        from app.analytics import signal_engine
        from app.brokers.mock import MockBroker
        b = MockBroker()
        sig = signal_engine.generate(b.candles(days=8), chain=b.option_chain(), india_vix=14.0)
        return f"{sig.action} at {sig.confidence:.0%}, {len(sig.checks)} checks"

    @check("risk manager approves a valid trade")
    def _():
        from datetime import date

        from app.risk.manager import DayState, RiskConfig, evaluate
        d = evaluate(config=RiskConfig(capital=500_000, lot_size=75),
                     state=DayState(trading_day=date.today()),
                     entry=24_000, stop_loss=23_970, target=23_940)
        if not d.approved:
            raise RuntimeError(f"blocked a valid trade: {d.reasons}")
        return f"{d.quantity} units ({d.lots} lots)"

    @check("backtest runs end to end")
    def _():
        from app.backtest.engine import run
        from app.brokers.mock import MockBroker
        from app.risk.manager import RiskConfig
        res = run(MockBroker().candles(days=10), starting_capital=500_000,
                  risk_config=RiskConfig(capital=500_000, lot_size=75))
        st = res.stats
        return f"{st.get('trades', 0)} trades, expectancy {st.get('expectancy_r', 0)}R"


def run_infrastructure() -> None:
    section("infrastructure")

    # Every infrastructure check imports app.config, so one missing package
    # would otherwise produce four identical failures and hide the real
    # state of your database. Gate on it and say what to run instead.
    try:
        import pydantic_settings  # noqa: F401
    except ImportError:
        record("infrastructure checks", SKIP,
               "Backend dependencies are not installed in this Python. Run:\n"
               "         python3 -m pip install -r backend/requirements-local.txt")
        return

    @check("settings load from environment")
    def _():
        from app.config import get_settings
        s = get_settings()
        return f"broker={s.broker} live_trading={s.live_trading} env={s.environment}"

    @check("postgres reachable and schema at head")
    def _():
        # Read-only. This used to call init_db(), which builds every table on
        # an empty database — a schema change from a diagnostic.
        from app import schema_check
        status = schema_check.check()
        if not status.ok:
            raise RuntimeError(status.message)
        return status.message

    @check("candle upsert is idempotent")
    def _():
        # Bars are built here rather than taken from the mock broker: the
        # mock steps forward five minutes at a time with no notion of
        # weekends or session hours, so the validation gate correctly
        # rejects most of them. Feeding it mock candles would test the
        # rejector, not the upsert, and report the failure as a duplication
        # bug — which is exactly the wrong place to go looking.
        from datetime import datetime, timedelta, timezone

        import pandas as pd
        from app.data.importer import import_index_candles
        from app.data.repository import load_index_candles
        from app.db import SessionLocal
        from app.models import CandleRecord

        # The most recent weekday, at 09:15 IST — a session that has ended.
        ist = timezone(timedelta(hours=5, minutes=30))
        day = datetime.now(ist).date() - timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        first = datetime(day.year, day.month, day.day, 9, 15, tzinfo=ist)

        df = pd.DataFrame([{
            "timestamp": pd.Timestamp(first + timedelta(minutes=5 * i)).tz_convert("UTC"),
            "open": 24_000.0 + i, "high": 24_010.0 + i,
            "low": 23_990.0 + i, "close": 24_005.0 + i, "volume": 1000.0 + i,
        } for i in range(20)])

        from app.migration_guard import writer_lease
        with writer_lease("doctor"), SessionLocal() as db:
            first_write = import_index_candles(db, df, "__DOCTOR__", "5m", "doctor")
            second_write = import_index_candles(db, df, "__DOCTOR__", "5m", "doctor")
            back = load_index_candles(db, "__DOCTOR__", "5m")
            db.query(CandleRecord).filter(CandleRecord.symbol == "__DOCTOR__").delete()
            db.commit()

        if first_write.write.inserted == 0:
            raise RuntimeError(
                "the validation gate rejected every test bar, so this check "
                f"proved nothing: {first_write.rejection.to_dict()['by_reason']}")
        if len(back) != first_write.write.inserted:
            raise RuntimeError(
                f"inserted {first_write.write.inserted} rows, wrote the same "
                f"batch again, and read back {len(back)} — the upsert is "
                "duplicating instead of updating")
        if second_write.write.inserted:
            raise RuntimeError(
                f"the second identical write inserted {second_write.write.inserted} "
                "new rows; it should have updated in place")
        return f"{len(back)} rows after two identical writes (correct)"

    @check("redis reachable")
    def _():
        from app.cache import client
        c = client()
        if not c:
            raise WarnCheck("Redis is not reachable; caching is disabled but the app still runs")
        return "ping ok"


def run_network() -> None:
    section("live data sources")

    @check("NSE option chain endpoint")
    def _():
        from app.brokers.nse import NSEClient, parse_option_chain
        client = NSEClient()
        payload = client.raw_option_chain("NIFTY")
        chain, spot = parse_option_chain(payload)
        client.close()
        if chain.empty:
            raise RuntimeError("NSE responded but the chain parsed empty")
        return (f"{len(chain)} strikes, spot {spot}, "
                f"expiries {payload['records'].get('expiryDates', [])[:2]}")

    @check("NSE India VIX")
    def _():
        from app.brokers.freedata import FreeDataBroker
        vix = FreeDataBroker().india_vix()
        if vix is None:
            raise RuntimeError("VIX came back None — check the index name in allIndices")
        return f"India VIX {vix}"

    @check("Yahoo 5m candles")
    def _():
        from app.brokers.freedata import FreeDataBroker
        df = FreeDataBroker().candles("NIFTY", "5m", days=5)
        if df.empty:
            raise RuntimeError("Yahoo returned an empty frame")
        zero_vol = df["volume"].nunique() == 1
        note = " (volume is synthetic — Yahoo reports none)" if zero_vol else ""
        return f"{len(df)} candles, last close {df['close'].iloc[-1]:.2f}{note}"

    @check("free broker feeds the analytics unchanged")
    def _():
        from app.analytics import signal_engine
        from app.brokers.freedata import FreeDataBroker
        b = FreeDataBroker()
        chain, _ = b.chain_with_spot("NIFTY")
        sig = signal_engine.generate(b.candles("NIFTY", "5m", days=10),
                                     chain=chain, india_vix=b.india_vix())
        return f"{sig.action} at {sig.confidence:.0%} on live data"


def run_api(base: str) -> None:
    section(f"running backend at {base}")
    try:
        import httpx
    except ImportError:
        record("api checks", SKIP, "httpx not installed")
        return

    for path in ("/health", "/health/broker", "/signals/live",
                 "/market/structure", "/data/coverage"):
        @check(f"GET {path}")
        def _(p=path):
            r = httpx.get(f"{base}{p}", timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:180]}")
            body = r.json()
            if not isinstance(body, dict):
                return json.dumps(body)[:180]
            return ", ".join(f"{k}={v}" for k, v in list(body.items())[:4])[:180]

    # Reported on its own rather than through the loop above, because the
    # generic printer truncates and the one field that matters here — what
    # is actually starving — would be the part cut off. `/health` said "ok"
    # throughout both sessions the desk lost; this is the question that was
    # really failing.
    @check("scheduler not starved")
    def _():
        r = httpx.get(f"{base}/health/scheduler", timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:180]}")
        body = r.json()
        jobs = body.get("jobs") or {}
        summary = ", ".join(
            f"{name} ok={h['successes']} skipped={h['skips']}"
            for name, h in sorted(jobs.items())) or "no job has run yet"
        if not body.get("healthy", True):
            raise RuntimeError("; ".join(body["problems"]))
        return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-network", action="store_true", help="skip NSE and Yahoo")
    parser.add_argument("--no-infra", action="store_true", help="skip Postgres and Redis")
    parser.add_argument("--api", nargs="?", const="http://localhost:8000",
                        help="also probe a running backend")
    args = parser.parse_args()

    print("QuantDesk doctor")
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {platform.platform()}")

    run_environment()
    run_analytics()
    if not args.no_infra:
        run_infrastructure()
    else:
        section("infrastructure")
        record("postgres / redis", SKIP, "--no-infra")
    if not args.no_network:
        run_network()
    else:
        section("live data sources")
        record("NSE / Yahoo", SKIP, "--no-network")
    if args.api:
        run_api(args.api.rstrip("/"))

    section("summary")
    counts = {s: sum(1 for _, st, _ in results if st == s) for s in (PASS, FAIL, WARN, SKIP)}
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, "
          f"{counts[WARN]} warnings, {counts[SKIP]} skipped")

    failures = [(n, d) for n, st, d in results if st == FAIL]
    if failures:
        print("\nFailures to fix:")
        for name, detail in failures:
            print(f"  - {name}: {detail}")
        print("\nPaste this whole output back and I will fix them.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
