"""The Nifty agent.

Runs on a schedule, reads price + chain + VIX, produces one signal, writes
it to the database, and publishes it to Redis for the dashboard to pick up.

It never places an order by itself. Execution stays a deliberate human act
until you have live statistics you trust.
"""
from __future__ import annotations

import json
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ..api.signals import build_signal
from ..cache import publish
from ..config import get_settings
from ..data import importer
from ..db import SessionLocal
from ..deps import get_broker
from ..market_hours import is_open as market_is_open
from ..models import SignalRecord
from ..risk import live as risk_live

log = logging.getLogger(__name__)


def tick() -> None:
    s = get_settings()
    if s.environment == "prod" and not market_is_open():
        return
    # Archive first. Even if analysis fails, the candles are worth keeping —
    # on a free data source, history you did not save is history you lose.
    if s.archive_candles:
        try:
            with SessionLocal() as db:
                importer.import_index_candles(
                    db,
                    get_broker().candles(s.watch_symbol, s.watch_timeframe, days=5),
                    s.watch_symbol, s.watch_timeframe, source=s.broker,
                )
        except Exception:
            log.exception("candle archiving failed")

    # The option chain is captured by `workers.option_collector`, not here.
    # It has to poll faster than the bar width to build a bar with a real
    # range, and the agent's cadence is the strategy's timeframe and should
    # not be driven by a data-collection need.

    try:
        sig = build_signal(s.watch_symbol, s.watch_timeframe)
    except Exception:
        log.exception("agent tick failed")
        return

    log.info("\n%s", sig.explain())

    payload = sig.to_dict()
    payload["explanation"] = sig.explain()

    with SessionLocal() as db:
        # The same assembly `/signals/live` uses. This route publishes the
        # signal the dashboard actually renders, and until audit finding H-4
        # it carried no risk block — so the kill switch, the trade cap, the
        # loss limit and the position cap were all invisible on screen.
        #
        # Decided before the row is written, so the stored signal and the
        # published one carry the same verdict rather than two evaluations
        # taken a moment apart.
        risk_live.attach(db, payload, sig)

        db.add(SignalRecord(
            symbol=sig.symbol, timeframe=sig.timeframe, action=sig.action,
            confidence=sig.confidence, price=sig.price, entry=sig.entry,
            stop_loss=sig.stop_loss, target=sig.target,
            checks=[c.to_dict() for c in sig.checks], context=sig.context,
            risk=payload["risk"],
        ))
        db.commit()

    publish_signal(payload)


def publish_signal(payload: dict) -> None:
    """Fan a signal out to the dashboard, refusing one that skipped risk.

    The guard is the point. A missing risk block is silent — the dashboard
    renders the trade plan and simply omits the line saying it was refused —
    so nothing downstream would report the regression this replaces.
    """
    risk_live.assert_evaluated(payload)
    publish("signals", json.dumps(payload, default=str),
            cache_key="signal:latest", ttl=900)


def start() -> BackgroundScheduler:
    s = get_settings()
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(tick, "interval", minutes=s.agent_interval_minutes,
                      id="nifty-agent", max_instances=1, coalesce=True)
    scheduler.start()
    log.info("Nifty agent scheduled every %s minutes.", s.agent_interval_minutes)
    return scheduler
