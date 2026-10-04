"""Refresh the dashboard's live-payload fixtures from a running desk.

The frontend has one suite that renders against real API responses rather
than hand-written fixtures, because a hand-written fixture is a statement
about what its author *believed* the backend returns. That belief goes stale,
and when it does the dashboard reads a field nothing sends and renders a
blank panel — which looks like a quiet market rather than a bug.

Run this whenever an endpoint the dashboard reads changes shape:

    python scripts/capture_live_payloads.py            # against localhost
    python scripts/capture_live_payloads.py --api http://host:8000

Read-only. It fetches and writes files; it changes nothing on the desk.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "fixtures" / "live"

# Exactly what the dashboard asks for, spelled the way it asks for it. A
# request the dashboard makes but this does not capture would silently fall
# through to an empty body in the suite.
PATHS = {
    "signals_live": "/signals/live?symbol=NIFTY&timeframe=5m",
    "market_status": "/market/status",
    "market_price": "/market/price",
    "market_regime": "/market/regime",
    "candles": "/market/candles?symbol=NIFTY&interval=5m&days=2",
    "chain": "/market/option-chain?symbol=NIFTY",
    "history": "/signals/history?limit=25",
    "outcomes": "/signals/outcomes?symbol=NIFTY&include_signals=true",
    "quality": "/data/quality?symbol=NIFTY",
    "scheduler": "/health/scheduler",
    "coverage": "/data/coverage?symbol=NIFTY",
    "vix": "/market/vix",
    "news": "/news",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0

    for name, path in PATHS.items():
        url = f"{args.api.rstrip('/')}{path}"
        try:
            with urllib.request.urlopen(url, timeout=args.timeout) as response:
                body = response.read()
            json.loads(body)          # refuse to file something unparseable
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"  {name:<15} FAILED  {exc}")
            failures += 1
            continue
        (OUT / f"{name}.json").write_bytes(body)
        print(f"  {name:<15} {len(body):>8} bytes")

    if failures:
        # Deliberately non-zero, and deliberately leaving the fixtures that
        # did land. A partial refresh is still better than a stale one, but
        # nobody should think it succeeded.
        print(f"\n{failures} endpoint(s) did not answer — fixtures are partial.")
        return 1
    print(f"\nWrote {len(PATHS)} fixtures to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
