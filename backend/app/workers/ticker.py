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

# How far behind the market a price may be before the desk should say so.
#
# Both numbers are set against the measured behaviour of the free sources:
# Yahoo's quote is typically 2-6s behind the print and occasionally 11s, so
# anything inside 15s is ordinary and calling it "delayed" would train the
# eye to ignore the warning. Past 60s the source has stopped refreshing —
# that is a real fault, not jitter.
LIVE_SECONDS = 15
DELAYED_SECONDS = 60


def classify_age(age_seconds: float | None) -> str:
    """Name how far behind the market a price is.

    Kept as a plain function of one number so it can be tested without a
    broker, a socket or a clock, and so the dashboard and the API cannot
    drift into disagreeing about what "stale" means.

    `None` means the source would not say when the price was printed. That
    is reported as "unknown" rather than "live": a price we cannot date is
    precisely the one that should not be presented as current.
    """
    if age_seconds is None:
        return "unknown"
    if age_seconds <= LIVE_SECONDS:
        return "live"
    if age_seconds <= DELAYED_SECONDS:
        return "delayed"
    return "stale"

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

    # Two different clocks, and conflating them is what made a stale price
    # look current:
    #   source_time — when the market printed this price. The only basis on
    #                 which staleness can honestly be judged.
    #   at          — when we published it. Useful for measuring our own
    #                 internal delay, and for a browser to correct its clock
    #                 against the server's rather than trusting its own.
    published = datetime.now(UTC)
    source_time = quote.get("source_time")
    age = None
    if source_time:
        try:
            age = round((published - datetime.fromisoformat(source_time)).total_seconds(), 3)
        except (TypeError, ValueError):
            log.debug("unparseable source_time %r", source_time)

    payload = {
        "symbol": symbol,
        "price": price,
        "previous": prev,
        "change": None if prev is None else round(price - prev, 2),
        "direction": "flat" if prev is None or price == prev
        else "up" if price > prev else "down",
        "source": quote.get("source", settings.broker),
        "source_time": source_time,
        "age_seconds": age,
        "freshness": classify_age(age),
        "at": published.isoformat(),
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
