"""Live price ticker — now the fallback rather than the source.

The agent runs every five minutes because that is the strategy's timeframe.
Between ticks the dashboard showed a frozen number, which is correct for a
signal and wrong for a price. This is the second, much lighter loop that
keeps the tape moving. It computes nothing and decides nothing; signals
still come only from the agent, so there is still exactly one code path that
produces a trading decision.

What changed: when the Angel One websocket is enabled and healthy, this job
**does not poll at all**. A push feed already delivered the price, and
polling a second source alongside it would spend requests to produce a
number that is strictly worse — a poll cannot be fresher than a push — while
racing it into the same Redis key. The two would take turns publishing, the
change column would flip between two slightly different prices, and the
`source` field would be the only clue.

So the rule is a single question asked once per job run: *is Angel serving
right now?* If yes, do nothing. If no, poll as before, publish with the
polling source's own name, and record that a fallback happened — so the
report can tell one long outage from a feed that keeps flapping.

Honest limits of the polled path, unchanged:

  - It is a poll, not a stream. NSE and Yahoo hand out snapshots; expect a
    few seconds of lag.
  - It runs only while the market is open. Polling a closed market wastes
    requests and risks throttling for nothing.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from .. import net
from ..config import get_settings
from ..deps import get_broker
from ..market_hours import is_open as market_is_open
from .prices import (  # noqa: F401  (re-exported: imported from here elsewhere)
    CACHE_KEY,
    CHANNEL,
    DELAYED_SECONDS,
    LIVE_SECONDS,
    classify_age,
    publish_price,
)

log = logging.getLogger(__name__)


def tick() -> None:
    settings = get_settings()
    if settings.environment == "prod" and not market_is_open():
        return

    # Asked once, here, rather than inside the publish path. A poll that is
    # started and then discarded has already spent the request and already
    # taken the slot that `max_instances=1` protects.
    # One question, asked of the feed itself: should the poller serve this
    # cycle? Previously this read `healthy()` directly and fell back the
    # instant a tick was late, which made the desk's source flip to Yahoo
    # and back on a single blip. The feed now debounces that decision and
    # counts what it suppressed; see `AngelFeed.should_poll`.
    from . import angel_feed
    if not angel_feed.should_poll():
        return

    symbol = settings.watch_symbol
    try:
        # Bounded by this job's own interval. A quote that takes longer than
        # the gap to the next poll is not a slow quote, it is a quote that
        # will be superseded before it arrives — and while it is in flight
        # `max_instances=1` skips the poll that would have replaced it.
        with net.budget(net.budget_for(settings.ticker_interval_seconds),
                        label="price-ticker"):
            quote = get_broker().quote(symbol)
        # Stamped the moment the response landed, before any parsing, so it
        # measures the network and not us. The push feed has always set
        # this; the poller did not, which left the two sources publishing
        # different shapes — the one thing `prices.publish_price` exists to
        # prevent. The gap was invisible until the dashboard began reading
        # `received_at`, at which point a fallback to the poller would have
        # silently dropped it back to ageing against the coarse
        # `source_time`.
        received_at = datetime.now(UTC)
        price = float(quote["last_price"])
    except Exception as exc:
        # A failed price poll is routine — NSE throttles, networks blip.
        # Log quietly and let the next tick try again rather than filling
        # the log with stack traces every ten seconds.
        log.debug("price tick failed: %s", exc)
        return

    publish_price(
        symbol, price,
        source=quote.get("source", settings.broker),
        source_time=quote.get("source_time"),
        received_at=received_at,
        transport="poll")


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
    # The job still runs at this cadence when Angel is enabled — it is what
    # notices the feed has gone quiet and takes over. It just stops making
    # requests while the feed is healthy.
    log.info("price ticker running every %ss (%s)",
             settings.ticker_interval_seconds,
             "fallback only — Angel feed is preferred" if settings.angel_enabled
             else "primary source")
    return scheduler
