"""Live price ticker.

The agent runs every five minutes because that is the strategy's timeframe.
Between ticks the dashboard showed a frozen number, which is correct for a
signal and wrong for a price — a desk should show the market moving.

So this is a second, much lighter loop: fetch the spot price every few
seconds and publish it. It computes nothing and decides nothing. Signals
still come only from the agent, so there is still exactly one code path
that produces a trading decision.

Honest limits of the free data:

  - This is a poll, not a stream. NSE and Yahoo hand out snapshots; neither
    gives retail a tick-by-tick websocket. Expect a few seconds of lag.
  - It runs only while the market is open. Polling a closed market wastes
    requests and risks getting your IP throttled for nothing.
  - The number can be several seconds stale. Fine for watching the market.
    Not fine for anything that needs an exact fill price.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from ..cache import client as redis_client
from ..config import get_settings
from ..deps import get_broker
from ..market_hours import is_open as market_is_open

log = logging.getLogger(__name__)

CHANNEL = "prices"
CACHE_KEY = "price:latest"

# Remembered between ticks so the dashboard can show direction and change
# without needing a second request for the previous value.
_previous: dict[str, float] = {}


def tick() -> None:
    settings = get_settings()
    if settings.environment == "prod" and not market_is_open():
        return

    symbol = settings.watch_symbol
    try:
        quote = get_broker().quote(symbol)
        price = float(quote["last_price"])
    except Exception as exc:
        # A failed price poll is routine — NSE throttles, networks blip.
        # Log quietly and let the next tick try again rather than filling
        # the log with stack traces every ten seconds.
        log.debug("price tick failed: %s", exc)
        return

    prev = _previous.get(symbol)
    _previous[symbol] = price

    payload = {
        "symbol": symbol,
        "price": price,
        "previous": prev,
        "change": None if prev is None else round(price - prev, 2),
        "direction": "flat" if prev is None or price == prev
        else "up" if price > prev else "down",
        "source": quote.get("source", settings.broker),
        "at": datetime.now(UTC).isoformat(),
        "market_open": market_is_open(),
    }

    r = redis_client()
    if r:
        blob = json.dumps(payload)
        r.setex(CACHE_KEY, 120, blob)
        r.publish(CHANNEL, blob)


def start(scheduler: BackgroundScheduler | None = None) -> BackgroundScheduler:
    settings = get_settings()
    scheduler = scheduler or BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(
        tick, "interval",
        seconds=settings.ticker_interval_seconds,
        id="price-ticker", max_instances=1, coalesce=True,
    )
    if not scheduler.running:
        scheduler.start()
    log.info("price ticker running every %ss", settings.ticker_interval_seconds)
    return scheduler