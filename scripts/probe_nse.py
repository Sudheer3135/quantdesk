#!/usr/bin/env python3
"""Probe NSE for a working option chain endpoint.

NSE moved the option chain path — /api/option-chain-indices now returns 404
while /api/allIndices still works, so the session and headers are fine and
only the path is wrong. Rather than guess, this tries every candidate and
reports what each one actually returns.

    python3 scripts/probe_nse.py

Paste the output back. Whichever path returns 200 with strike data becomes
the new default.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.brokers.nse import NSEClient  # noqa: E402


def describe(payload: object) -> str:
    """Say what came back without dumping megabytes of JSON."""
    if not isinstance(payload, dict):
        return f"{type(payload).__name__}, not a dict"

    top = list(payload.keys())[:6]
    records = payload.get("records") or payload.get("data") or {}

    if isinstance(records, dict):
        rows = records.get("data", [])
        spot = records.get("underlyingValue")
        expiries = records.get("expiryDates", [])[:2]
        if rows:
            sample = list(rows[0].keys())[:8]
            return (f"keys={top} rows={len(rows)} spot={spot} "
                    f"expiries={expiries} row_keys={sample}")
    if isinstance(records, list) and records:
        return f"keys={top} rows={len(records)} row_keys={list(records[0].keys())[:8]}"

    return f"keys={top} (no recognisable strike rows)"


def main() -> int:
    client = NSEClient()
    print("Probing NSE option chain endpoints\n")

    working = []
    try:
        contract = client.contract_info("NIFTY")
    except Exception as exc:
        print("  --   /api/option-chain-contract-info?symbol=NIFTY")
        print(f"       {exc}")
        client.close()
        return 1

    expiries = contract.get("expiryDates") or []
    print("  200  /api/option-chain-contract-info?symbol=NIFTY")
    print(f"       expiryDates={expiries[:4]}")

    for expiry in expiries:
        url = f"/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry={expiry}"
        try:
            payload = client.get_json(url, attempts=1)
            if isinstance(payload, dict) and payload.get("records", {}).get("data"):
                print(f"  200  {url}")
                print(f"       {describe(payload)}")
                working.append((url, payload))
                break
            print(f"  --   {url}")
            print(f"       {describe(payload)}")
        except Exception as exc:
            message = str(exc).split(":")[-1].strip()
            print(f"  --   {url}")
            print(f"       {message}")

    print("\n--- control (this one already works) ---")
    try:
        indices = client.all_indices()
        names = [r.get("index") for r in indices.get("data", [])][:4]
        print(f"  200  /api/allIndices  -> {names}")
    except Exception as exc:
        print(f"  --   /api/allIndices  -> {exc}")

    client.close()

    print("\n" + "-" * 60)
    if working:
        url, payload = working[0]
        print(f"Working endpoint: {url}")
        out = ROOT / "tests" / "fixtures"
        out.mkdir(parents=True, exist_ok=True)
        records = payload.get("records", {})
        trimmed = {
            "records": {
                "underlyingValue": records.get("underlyingValue"),
                "expiryDates": records.get("expiryDates", [])[:1],
                # Do not re-filter by expiryDate. The v3 endpoint already
                # filtered server-side and its rows carry no such field, so
                # this filter is what saved an empty fixture from a payload
                # that had 113 rows in it.
                "data": records.get("data", [])[:12],
            },
            "_endpoint": url,
        }
        target = out / "nse_chain_probe.json"
        target.write_text(json.dumps(trimmed, indent=1, default=str))
        print(f"Sample saved to {target.relative_to(ROOT)} — attach it and I will "
              "write tests against the real shape.")
        return 0

    print("No candidate worked. Paste this output back — the next step is to")
    print("open https://www.nseindia.com/option-chain in Chrome, press F12,")
    print("open the Network tab, reload, and read off the request the page")
    print("itself makes. That is always the definitive answer.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
