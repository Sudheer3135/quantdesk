#!/usr/bin/env python3
"""Record real API responses as test fixtures.

This is how you hand me real data without me needing network access. Run it
once during market hours; it saves what NSE and Yahoo actually returned into
tests/fixtures/. Attach those files to a message and every parser in this
codebase can then be tested against the real shape instead of my guess at it.

    python3 scripts/record_fixtures.py

The saved files contain public market data only — no account details, no
credentials, no personal information. Read them before sharing if you want
to check.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT = ROOT / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)


def save_json(name: str, payload: object) -> Path:
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=1, default=str))
    size = path.stat().st_size / 1024
    print(f"  saved {path.relative_to(ROOT)}  ({size:.0f} KB)")
    return path


def trim_option_chain(payload: dict, keep_expiries: int = 2) -> dict:
    """NSE's full payload is a few MB. Keep the nearest expiries only —
    that is enough to test every parsing branch and small enough to attach."""
    records = payload.get("records", {})
    expiries = list(records.get("expiryDates", []))[:keep_expiries]
    rows = [r for r in records.get("data", []) if r.get("expiryDate") in expiries]
    return {
        "records": {
            "underlyingValue": records.get("underlyingValue"),
            "expiryDates": expiries,
            "timestamp": records.get("timestamp"),
            "data": rows,
        },
        "_recorded_at": datetime.now(UTC).isoformat(),
        "_note": f"trimmed to {keep_expiries} expiries from the full NSE payload",
    }


def main() -> int:
    print("Recording fixtures. Best run during market hours (09:15-15:30 IST).\n")
    failures = []

    print("NSE option chain")
    try:
        from app.brokers.nse import NSEClient
        client = NSEClient()
        raw = client.raw_option_chain("NIFTY")
        save_json("nse_option_chain_nifty", trim_option_chain(raw))
        records = raw.get("records", {})
        print(f"  spot {records.get('underlyingValue')}, "
              f"{len(records.get('data', []))} rows, "
              f"expiries {records.get('expiryDates', [])[:3]}")
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        failures.append(("nse option chain", exc))
        client = None

    print("\nNSE all indices")
    try:
        from app.brokers.nse import NSEClient
        client = client or NSEClient()
        save_json("nse_all_indices", client.all_indices())
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        failures.append(("nse all indices", exc))

    if client:
        client.close()

    print("\nYahoo 5-minute candles")
    try:
        import yfinance as yf
        raw = yf.download("^NSEI", period="5d", interval="5m",
                          progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            raise RuntimeError("empty frame")
        # Record the shape as well as the data — the MultiIndex column
        # behaviour is exactly the thing that breaks between versions.
        meta = {
            "yfinance_version": getattr(yf, "__version__", "unknown"),
            "columns": [str(c) for c in raw.columns],
            "is_multiindex": bool(hasattr(raw.columns, "levels")),
            "index_name": str(raw.index.name),
            "index_tz": str(getattr(raw.index, "tz", None)),
            "rows": len(raw),
        }
        save_json("yahoo_nsei_5m_meta", meta)
        flat = raw.copy()
        if hasattr(flat.columns, "levels"):
            flat.columns = flat.columns.get_level_values(0)
        flat.reset_index().tail(120).to_csv(OUT / "yahoo_nsei_5m.csv", index=False)
        print(f"  saved tests/fixtures/yahoo_nsei_5m.csv  "
              f"({meta['rows']} rows fetched, last 120 kept)")
        print(f"  columns: {meta['columns'][:6]}  multiindex={meta['is_multiindex']}")
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        failures.append(("yahoo candles", exc))

    print("\n" + "-" * 60)
    if failures:
        print(f"{len(failures)} source(s) failed:")
        for name, exc in failures:
            print(f"  - {name}: {type(exc).__name__}: {exc}")
        print("\nPaste these errors back — the fix is usually in the request "
              "headers or the endpoint path, not the parser.")
    else:
        print("All fixtures recorded. Attach the files in tests/fixtures/ "
              "and I can write tests against real data shapes.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
