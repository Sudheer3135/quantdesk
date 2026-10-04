"""Tests for the free data path.

The network calls cannot be tested here, but the parsing can — and parsing
is where this kind of adapter actually breaks. The fixture below matches the
shape NSE returns from /api/option-chain-indices: one record per strike,
each holding optional CE and PE blocks, with several expiries mixed in.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import options
from app.brokers.nse import expiry_dates, parse_index_value, parse_option_chain


def make_leg(oi, change, volume, iv, ltp):
    return {
        "openInterest": oi, "changeinOpenInterest": change,
        "totalTradedVolume": volume, "impliedVolatility": iv, "lastPrice": ltp,
    }


@pytest.fixture
def nse_payload():
    strikes = [24_300, 24_400, 24_500, 24_600, 24_700]
    data = []
    for i, strike in enumerate(strikes):
        for expiry in ("07-Aug-2026", "28-Aug-2026"):
            data.append({
                "strikePrice": strike,
                "expiryDate": expiry,
                "CE": make_leg(900_000 - i * 50_000, 12_000 * (i - 2), 400_000,
                               13.5, max(24_520 - strike, 0) + 45),
                "PE": make_leg(700_000 + i * 60_000, -8_000 * (i - 2), 350_000,
                               15.2, max(strike - 24_520, 0) + 52),
            })
    # A strike where one side has not traded at all — this is common and has
    # crashed more than one option chain parser.
    data.append({"strikePrice": 24_800, "expiryDate": "07-Aug-2026",
                 "CE": make_leg(120_000, 4_000, 20_000, 12.1, 8.5)})
    return {
        "records": {
            "underlyingValue": 24_521.35,
            "expiryDates": ["07-Aug-2026", "28-Aug-2026"],
            "data": data,
        }
    }


def test_parses_spot_and_nearest_expiry_only(nse_payload):
    chain, spot = parse_option_chain(nse_payload)
    assert spot == pytest.approx(24_521.35)
    # 5 two-sided strikes + 1 call-only strike, second expiry excluded
    assert len(chain) == 6
    assert list(chain["strike"]) == sorted(chain["strike"])


def test_missing_leg_is_unavailable_not_zero_and_not_a_crash(nse_payload):
    """Rewritten in Pass 2C (OC-5). This used to assert the missing PE leg
    became `put_oi == 0` — the zero-fill that let a one-sided chain read as
    a put/call ratio of 0.0, a bearish vote on no data. A leg NSE did not
    send is NaN: parsed without crashing, and not mistaken for a recorded
    zero."""
    chain, _ = parse_option_chain(nse_payload)
    row = chain[chain["strike"] == 24_800].iloc[0]
    assert row["call_oi"] == 120_000
    assert pd.isna(row["put_oi"])
    # The missing leg's price is missing too — not a zero premium. Restored
    # in 2C.1: the 2C rewrite dropped this assertion instead of inverting it.
    assert pd.isna(row["put_ltp"])
    assert row["put_ltp"] != 0
    for column in ("put_bid", "put_ask", "put_volume", "put_iv"):
        assert pd.isna(row[column]), column


def test_can_select_a_later_expiry(nse_payload):
    chain, _ = parse_option_chain(nse_payload, expiry="28-Aug-2026")
    assert len(chain) == 5          # the call-only strike is near-expiry only


def test_unknown_expiry_raises(nse_payload):
    with pytest.raises(ValueError):
        parse_option_chain(nse_payload, expiry="01-Jan-2030")


def test_empty_payload_raises():
    with pytest.raises(ValueError):
        parse_option_chain({"records": {"data": []}})


def test_parsed_chain_feeds_the_analytics_unchanged(nse_payload):
    """The whole point of the adapter: NSE data must drop straight into the
    same analytics that the mock and Kite adapters feed."""
    chain, spot = parse_option_chain(nse_payload)
    summary = options.summarise(chain, spot)
    assert summary.pcr_oi > 0
    assert chain["strike"].min() <= summary.max_pain <= chain["strike"].max()
    assert summary.bias in {"bullish", "bearish", "neutral"}


def test_expiry_dates_listed(nse_payload):
    assert expiry_dates(nse_payload) == ["07-Aug-2026", "28-Aug-2026"]


def test_index_lookup_is_case_and_space_tolerant():
    payload = {"data": [
        {"index": "NIFTY 50", "last": 24_521.35},
        {"index": " INDIA VIX ", "last": 12.84},
    ]}
    assert parse_index_value(payload, "india vix") == pytest.approx(12.84)
    assert parse_index_value(payload, "NIFTY 50") == pytest.approx(24_521.35)
    assert parse_index_value(payload, "NOT AN INDEX") is None


def _leg(oi=1000):
    return {"openInterest": oi, "changeinOpenInterest": 100,
            "totalTradedVolume": 5000, "impliedVolatility": 13.2, "lastPrice": 50.0}


def test_v3_payload_without_expiry_on_rows_is_not_emptied():
    """The v3 endpoint takes expiry as a query parameter, so it filters
    server-side and its rows carry no expiryDate. Filtering again on a field
    that is not present silently discarded every strike — the probe found
    113 rows from NSE and saved a fixture containing zero.
    """
    payload = {"records": {
        "underlyingValue": 24614.9,
        "expiryDates": ["04-Aug-2026"],
        "data": [{"strikePrice": s, "CE": _leg(), "PE": _leg(900)}
                 for s in range(24000, 25200, 50)],
    }}
    chain, spot = parse_option_chain(payload)
    assert len(chain) == 24
    assert spot == pytest.approx(24614.9)


def test_legacy_payload_still_filters_by_expiry():
    payload = {"records": {
        "underlyingValue": 24614.9,
        "expiryDates": ["04-Aug-2026", "28-Aug-2026"],
        "data": [{"strikePrice": s, "expiryDate": e, "CE": _leg(), "PE": _leg(900)}
                 for e in ("04-Aug-2026", "28-Aug-2026")
                 for s in range(24000, 25200, 50)],
    }}
    assert len(parse_option_chain(payload)[0]) == 24
    assert len(parse_option_chain(payload, expiry="28-Aug-2026")[0]) == 24


def test_expiry_format_mismatch_degrades_instead_of_vanishing():
    """If NSE changes the date format, keep the rows and warn rather than
    return an empty chain that looks like an outage."""
    payload = {"records": {
        "underlyingValue": 24614.9,
        "expiryDates": ["04-Aug-2026"],
        "data": [{"strikePrice": s, "expiryDate": "2026-08-04",
                  "CE": _leg(), "PE": _leg(900)}
                 for s in range(24000, 25200, 50)],
    }}
    assert len(parse_option_chain(payload)[0]) == 24


def test_every_module_is_syntactically_valid():
    """A syntax error in a module nothing else imports will pass the whole
    suite otherwise — that is exactly how a stray quote in freedata.py
    survived 26 green tests.

    This parses every module rather than importing it, so it catches broken
    syntax without needing every third-party package installed.
    """
    import ast

    backend = Path(__file__).resolve().parents[1] / "backend"
    for name in [
        "app.analytics.indicators", "app.analytics.options",
        "app.analytics.signal_engine", "app.analytics.smc",
        "app.analytics.structure", "app.brokers.base", "app.brokers.mock",
        "app.brokers.nse", "app.brokers.freedata", "app.risk.manager",
        "app.backtest.engine", "app.api.market", "app.api.signals",
        "app.workers.archiver", "app.workers.agent", "app.models",
    ]:
        path = backend / (name.replace(".", "/") + ".py")
        assert path.exists(), f"{name} is missing at {path}"
        ast.parse(path.read_text(), filename=str(path))


def test_weekend_and_after_hours_candles_are_rejected():
    """The mock broker steps five minutes at a time with no notion of
    weekends, so its bars land on valid boundaries on a Sunday afternoon.
    Once in the archive they are invisible — 750 of them reached a real
    archive this way."""
    import pandas as pd
    from app.analytics.indicators import drop_outside_session

    stamps = pd.to_datetime([
        "2026-08-07T04:00:00Z",   # Fri 09:30 IST — keep
        "2026-08-07T09:55:00Z",   # Fri 15:25 IST — keep
        "2026-08-07T10:55:00Z",   # Fri 16:25 IST — after close
        "2026-08-07T03:30:00Z",   # Fri 09:00 IST — before open
        "2026-08-08T05:00:00Z",   # Saturday
        "2026-08-09T05:00:00Z",   # Sunday
    ], utc=True)
    df = pd.DataFrame({"timestamp": stamps, "open": 1.0, "high": 1.0,
                       "low": 1.0, "close": 1.0, "volume": 1.0})

    kept = drop_outside_session(df)
    assert len(kept) == 2
    assert kept["timestamp"].dt.tz_convert("Asia/Kolkata").dt.dayofweek.max() < 5


def test_mock_broker_candles_are_mostly_rejected():
    """Proof the guard catches the actual source of the contamination."""
    from app.analytics.indicators import drop_outside_session
    from app.brokers.mock import MockBroker

    raw = MockBroker().candles(days=10)
    kept = drop_outside_session(raw)
    assert len(kept) < len(raw), "mock candles should not all survive"


def test_mock_broker_rejects_unknown_intervals():
    """`interval="5m"` fell through to 375 bars a session, producing 22,125
    candles for a 59-day request with timestamps two months in the future —
    all of which reached a real archive."""
    from app.brokers.mock import MockBroker

    b = MockBroker()
    assert len(b.candles(days=10, interval="5m")) == \
           len(b.candles(days=10, interval="5minute"))

    with pytest.raises(ValueError, match="unknown interval"):
        b.candles(interval="not-a-timeframe")


def test_no_candle_may_be_dated_in_the_future():
    import pandas as pd
    from app.analytics.indicators import drop_future

    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame({
        "timestamp": [now - pd.Timedelta(days=1), now + pd.Timedelta(days=30)],
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1.0, 1.0],
    })
    assert len(drop_future(df)) == 1


def test_mock_candles_never_reach_the_archive_shape():
    """End to end: whatever the mock broker produces, the archive guards
    reject weekends, after-hours bars and anything future-dated."""
    import pandas as pd
    from app.analytics.indicators import (
        drop_future,
        drop_outside_session,
        drop_unclosed,
    )
    from app.brokers.mock import MockBroker

    df = MockBroker().candles(days=30, interval="5m")
    clean = drop_future(drop_outside_session(drop_unclosed(df, "5m")))

    local = clean["timestamp"].dt.tz_convert("Asia/Kolkata")
    assert (local.dt.dayofweek < 5).all()
    assert clean["timestamp"].max() <= pd.Timestamp.now(tz="UTC")


def test_archive_requires_an_explicit_source():
    """`source` defaulted to "free", so the backfill endpoint — which never
    passed it — labelled 19,000 mock candles as real data. The column that
    distinguished trustworthy rows from junk became useless precisely when
    it mattered. A forgotten argument must fail loudly.

    Checked on both the shim and the importer that replaced it: the rule
    protects the column, not one particular function."""
    import ast

    backend = Path(__file__).resolve().parents[1] / "backend"
    targets = {
        "app/workers/archiver.py": "archive",
        "app/data/importer.py": "import_index_candles",
    }

    for rel, function in targets.items():
        tree = ast.parse((backend / rel).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == function)
        names = [a.arg for a in fn.args.args]
        # defaults align to the tail of the argument list
        defaults = dict(zip(names[len(names) - len(fn.args.defaults):],
                            fn.args.defaults, strict=True))
        assert "source" in names, f"{rel}:{function} lost its source argument"
        assert "source" not in defaults, \
            f"{rel}:{function} gave source a default value"


def test_every_archive_caller_passes_a_source():
    """A signature check only helps if nothing calls it wrongly. This walks
    the actual call sites.

    The caller list is deliberately explicit and asserted non-empty. When
    the write path moved into `app/data`, a list naming only the old modules
    would have kept passing while checking nothing at all — and a vacuous
    test is worse than a deleted one, because it still reads as coverage."""
    import ast

    backend = Path(__file__).resolve().parents[1] / "backend"
    callers = [
        "app/api/market.py",
        "app/api/data.py",
        "app/workers/agent.py",
        "app/workers/archiver.py",
    ]
    writers = {"archive", "import_index_candles", "import_option_snapshot"}

    seen = 0
    for rel in callers:
        tree = ast.parse((backend / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in writers:
                continue
            seen += 1
            passed = {kw.arg for kw in node.keywords}
            assert "source" in passed or len(node.args) >= 5, \
                f"{rel} calls {name}() without a source"

    assert seen >= 3, (
        f"only found {seen} write call sites — the caller list has gone stale "
        "and this test is no longer checking anything")


def test_market_hours_has_one_definition():
    """Open and close times lived in three modules at once — the agent, the
    ticker and the stream. Three copies is three chances to disagree, and
    the first symptom would be the dashboard calling the market open while
    the agent had already stopped for the day.

    The regex also catches a bare (9, 15) / (15, 30) tuple, not only a
    `time(9, 15)` constructor call. Both `indicators.py` and
    `optionbuy/contracts.py` restated the session boundary as one of these
    and slipped past a version of this guard that only looked for the
    constructor form — an escape route this closes rather than a
    hypothetical one."""
    import re

    backend = Path(__file__).resolve().parents[1] / "backend"
    offenders = []
    pattern = re.compile(
        r"time\(\s*9,\s*15\s*\)|time\(\s*15,\s*30\s*\)"
        r"|[=(]\s*\(\s*9,\s*15\s*\)|[=(]\s*\(\s*15,\s*30\s*\)")
    for path in (backend / "app").rglob("*.py"):
        if path.name == "market_hours.py":
            continue
        if pattern.search(path.read_text()):
            offenders.append(path.name)
    assert not offenders, f"market hours redefined in: {offenders}"


def test_ticker_only_runs_during_market_hours():
    """Polling a closed market wastes requests and risks throttling for
    nothing. Weekends and out-of-hours must read as closed."""
    from datetime import datetime, timedelta, timezone

    from app.market_hours import is_open as market_is_open

    ist = timezone(timedelta(hours=5, minutes=30))
    cases = {
        datetime(2026, 8, 5, 11, 0, tzinfo=ist): True,    # Wed midday
        datetime(2026, 8, 5, 9, 15, tzinfo=ist): True,    # Wed at the open
        datetime(2026, 8, 5, 15, 30, tzinfo=ist): True,   # Wed at the close
        datetime(2026, 8, 5, 16, 40, tzinfo=ist): False,  # Wed after close
        datetime(2026, 8, 5, 8, 30, tzinfo=ist): False,   # Wed pre-open
        datetime(2026, 8, 8, 11, 0, tzinfo=ist): False,   # Saturday
        datetime(2026, 8, 9, 11, 0, tzinfo=ist): False,   # Sunday
    }
    for when, expected in cases.items():
        assert market_is_open(when) is expected, when


def test_market_status_labels_the_session():
    from datetime import datetime, timedelta, timezone

    from app.market_hours import status as market_status

    ist = timezone(timedelta(hours=5, minutes=30))
    assert market_status(datetime(2026, 8, 5, 11, 0, tzinfo=ist))["session"] == "open"
    assert market_status(datetime(2026, 8, 5, 17, 0, tzinfo=ist))["session"] == "closed"

    # 08:00 used to read "pre-open" because anything before 09:15 did — the
    # bug that made midnight announce an imminent session. Pre-open is now a
    # bounded window; 08:00 is simply closed, and 08:50 is not.
    assert market_status(datetime(2026, 8, 5, 8, 0, tzinfo=ist))["session"] == "closed"
    assert market_status(datetime(2026, 8, 5, 8, 50, tzinfo=ist))["session"] == "pre-open"

    # Weekends are closed like any other non-session; which kind of closed
    # moved to the `reason` field so the badge can stay a clean three-way.
    weekend = market_status(datetime(2026, 8, 8, 11, 0, tzinfo=ist))
    assert weekend["session"] == "closed"
    assert weekend["reason"] == "weekend"
