#!/usr/bin/env python3
"""Measure the Angel One feed against a live market.

Everything else about this integration is tested against fakes, which is
right: a socket that only worked at 09:20 on a weekday would be a socket
nobody could test. But fakes cannot tell you the two things that decide
whether the feed is worth having — how far behind the market Angel's ticks
actually are, and whether the timestamp decodes to the right instant.

So this runs against the real feed and reports numbers, not a pass or fail:

    ticks received          did it push at all
    distinct prices         or did it push the same number repeatedly
    source age p50/p95      how far behind the exchange the ticks are
    backend latency         our own share of that: parse plus publish
    redis/socket latency    publish to a browser actually holding it
    reconnects, stale       whether it stayed up

Run it during market hours. Outside them the exchange pushes nothing, and a
zero-tick result says nothing about the feed.

    ANGEL_ENABLED=true python3 scripts/angel_smoke.py --seconds 120

Reads credentials from the environment and `.env`, exactly as the backend
does. It never prints them.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

DEFAULT_SECONDS = 120
DEFAULT_API = "http://localhost:8000"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summarise(name: str, values: list[float], unit: str = "ms") -> str:
    if not values:
        return f"  {name:<26} no samples"
    return (f"  {name:<26} p50 {percentile(values, 50):>8} {unit}"
            f"   p95 {percentile(values, 95):>8} {unit}"
            f"   max {round(max(values), 1):>8} {unit}   n={len(values)}")


def run_direct(seconds: int) -> dict:
    """Drive the feed in this process and measure what arrives.

    Direct rather than through the running backend, so a failure is
    attributable: if this works and the container does not, the difference
    is the container.
    """
    from app.config import get_settings
    from app.workers import prices
    from app.workers.angel_feed import AngelFeed

    settings = get_settings()
    if not settings.angel_enabled:
        return {"error": "ANGEL_ENABLED is not true — nothing to measure."}

    samples: list[dict] = []

    def capture(symbol, price, **kw):
        payload = prices.publish_price(symbol, price, **kw)
        samples.append(payload)
        return payload

    feed = AngelFeed(publish_fn=capture)
    started = time.monotonic()
    if not feed.start():
        return {"error": feed.stats.last_error or "the feed refused to start"}

    print(f"listening for {seconds}s…", flush=True)
    try:
        while time.monotonic() - started < seconds:
            time.sleep(1)
            elapsed = int(time.monotonic() - started)
            if elapsed % 15 == 0:
                print(f"  {elapsed:>4}s  ticks={feed.stats.ticks:<6}"
                      f" published={feed.stats.published:<6}"
                      f" last={feed.stats.last_price}", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted — reporting what was collected.")
    finally:
        feed.stop()

    return {"feed": feed.status(), "samples": samples,
            "seconds": round(time.monotonic() - started, 1)}


def report(result: dict) -> int:
    if "error" in result:
        print(f"\nCould not measure: {result['error']}")
        return 1

    status = result["feed"]
    samples = result["samples"]
    counters = status["counters"]

    ages = [s["age_seconds"] * 1000 for s in samples
            if s.get("age_seconds") is not None]
    feed_latency = [s["feed_latency_ms"] for s in samples
                    if s.get("feed_latency_ms") is not None]
    publish_latency = [s["publish_latency_ms"] for s in samples
                       if s.get("publish_latency_ms") is not None]
    prices_seen = {s["price"] for s in samples}

    print("\n" + "=" * 68)
    print(f"ANGEL FEED SMOKE TEST — {result['seconds']}s")
    print("=" * 68)

    print(f"\n  ticks received             {counters['ticks']}")
    print(f"  published                  {counters['published']}"
          f"  (throttled {counters['throttled']})")
    print(f"  distinct prices            {len(prices_seen)}")
    if prices_seen:
        print(f"  price range                {min(prices_seen)} … {max(prices_seen)}")
    rate = counters["ticks"] / result["seconds"] if result["seconds"] else 0
    print(f"  tick rate                  {rate:.2f}/s")

    print("\n  LATENCY")
    print(summarise("source age (exchange→us)", ages))
    print(summarise("feed latency", feed_latency))
    print(summarise("backend latency", publish_latency))

    print("\n  STABILITY")
    for name in ("connects", "reconnects", "resubscribes", "logins",
                 "login_failures", "socket_errors", "malformed",
                 "stale_events", "fallbacks"):
        print(f"  {name:<26} {counters[name]}")

    print(f"\n  state                      {status['state']}")
    print(f"  healthy at exit            {status['healthy']}")
    if status["last_error"]:
        print(f"  last error                 {status['last_error']}")

    # The verdict is about whether the measurement is usable, not about
    # whether the numbers are good — that is a judgement for whoever reads
    # them, and a script that says PASS teaches people to stop reading.
    print("\n  " + "-" * 64)
    if counters["ticks"] == 0:
        print("  NO TICKS. Either the market is shut, the token is wrong, or")
        print("  the subscription never landed. Check `state` above: 'live'")
        print("  with no ticks means subscribed-and-silent, which is the")
        print("  worst case — the socket looks fine and delivers nothing.")
        return 1
    if ages and percentile(ages, 50) is not None and percentile(ages, 50) < 0:
        print("  NEGATIVE SOURCE AGE. Ticks appear to arrive before they were")
        print("  printed, which means the exchange timestamp is being decoded")
        print("  in the wrong zone or the wrong units.")
        return 1
    print("  Measurement complete. Read the numbers above rather than")
    print("  trusting this line — nothing here is a pass mark.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS,
                        help="how long to listen (default 120)")
    parser.add_argument("--json", action="store_true",
                        help="print the raw status block as JSON as well")
    args = parser.parse_args()

    from app.market_hours import status as market_status
    market = market_status()
    print(f"market: {market['session']} "
          f"({'open' if market['open'] else 'closed'}) "
          f"at {datetime.now(UTC).astimezone().strftime('%H:%M:%S %Z')}")
    if not market["open"]:
        print("NOTE: the exchange is shut. Angel will push little or nothing,\n"
              "      and a zero-tick result says nothing about the feed.")

    result = run_direct(args.seconds)
    code = report(result)
    if args.json and "feed" in result:
        print("\n" + json.dumps(result["feed"], indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
