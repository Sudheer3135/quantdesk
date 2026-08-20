"""Option-chain collector.

Separated from the agent because the two have genuinely different jobs and
therefore different cadences. The agent produces one trading decision every
five minutes, which is the strategy's timeframe and should not change. This
only records what the option market looked like, and it needs to run *more
often* than the bar width — otherwise every bar holds a single observation,
its high and low equal its close, and the range is fiction.

Polling every 60 seconds into 5-minute buckets gives roughly five
observations per bar: the first sets the open, the extremes track the high
and low, the last sets the close. None of that aggregation lives here; it is
already in `data/importer.import_option_snapshot`, which merges by design so
that repeated polls converge instead of duplicating.

Why this cadence and not faster: NSE's rate limits are undocumented and they
do block. Measured on 17-Aug-2026, six consecutive chain requests at 60s —
concurrent with the 10-second price ticker — all succeeded at about 1.6s
latency with no throttling. That is the evidence for 60s. There is none for
anything shorter, so do not shorten it without repeating the measurement.

The honest limit that remains: these are still *sampled* last-traded prices,
not a traded tape. Five samples make the range less fictional, not real.
Bars stay labelled `bar_kind='snapshot'` and carry a `samples` count so
nothing downstream can mistake them for true OHLC.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import get_settings
from ..data import importer
from ..db import SessionLocal
from ..deps import get_broker
from ..market_hours import is_open as market_is_open

log = logging.getLogger(__name__)


def capture(force: bool = False) -> dict | None:
    """Take one snapshot and fold it into the current bar.

    Returns the import report, or None when there was nothing to do.
    `force` bypasses the market-hours gate for manual and diagnostic use;
    the resulting bar is still stored and still flagged as out-of-hours by
    the importer, never silently discarded.
    """
    settings = get_settings()

    if not settings.archive_option_chain:
        return None

    # Polling a closed market wastes requests and risks being throttled for
    # data that has not changed since the close.
    if not force and not market_is_open():
        return None

    broker = get_broker()
    if not hasattr(broker, "chain_with_spot"):
        log.debug("%s cannot serve an option chain; skipping", type(broker).__name__)
        return None

    chain, spot = broker.chain_with_spot(settings.watch_symbol)

    # Refuse to file a snapshot whose expiry cannot be named. The chain
    # endpoint returns the nearest expiry when none is requested, and a
    # snapshot filed under a guess silently merges two different contracts
    # into one premium series — which then looks entirely plausible.
    expiry = chain.attrs.get("expiry")
    if not expiry:
        log.warning("option chain carried no expiry label; snapshot skipped")
        return None

    with SessionLocal() as db:
        report = importer.import_option_snapshot(
            db, chain,
            underlying=settings.watch_symbol,
            expiry=expiry,
            spot=spot,
            source=settings.broker,
            timeframe=settings.watch_timeframe,
            lot_size=settings.lot_size,
        )
    return report.to_dict()


def tick() -> None:
    try:
        capture()
    except Exception:
        # A failed poll costs one observation. Raising here would let
        # APScheduler retire the job, which would cost every observation
        # after it — and option history cannot be recovered later.
        log.exception("option chain snapshot failed")


def start(scheduler: BackgroundScheduler | None = None) -> BackgroundScheduler:
    settings = get_settings()
    scheduler = scheduler or BackgroundScheduler(timezone="Asia/Kolkata")

    seconds = settings.option_snapshot_interval_seconds
    if seconds <= 0:
        log.warning("option_snapshot_interval_seconds is %s; collector disabled", seconds)
        return scheduler

    scheduler.add_job(
        tick, "interval", seconds=seconds, id="option-collector",
        max_instances=1,
        # Skip missed runs rather than queueing them. A backlog would fire
        # several polls back to back, which is exactly the burst NSE blocks
        # for — and the skipped snapshots are stale by then anyway.
        coalesce=True,
    )
    if not scheduler.running:
        scheduler.start()

    log.info("option chain collector running every %ss (bars are %s buckets)",
             seconds, settings.watch_timeframe)
    return scheduler
