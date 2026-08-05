"""The Nifty agent.

Runs on a schedule, reads price + chain + VIX, produces one signal, writes
it to the database, and publishes it to Redis for the dashboard to pick up.

It never places an order by itself. Execution stays a deliberate human act
until you have live statistics you trust.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from ..api.signals import build_signal
from ..cache import client as redis_client
from ..config import get_settings
from ..db import SessionLocal
from ..deps import get_broker
from ..models import SignalRecord
from . import archiver

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)


def market_is_open(now: datetime | None = None) -> bool:
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def tick() -> None:
    s = get_settings()
    if s.environment == "prod" and not market_is_open():
        return
    # Archive first. Even if analysis fails, the candles are worth keeping —
    # on a free data source, history you did not save is history you lose.
    if s.archive_candles:
        try:
            with SessionLocal() as db:
                archiver.archive(
                    db,
                    get_broker().candles(s.watch_symbol, s.watch_timeframe, days=5),
                    s.watch_symbol, s.watch_timeframe, source=s.broker,
                )
        except Exception:
            log.exception("candle archiving failed")

    try:
        sig = build_signal(s.watch_symbol, s.watch_timeframe)
    except Exception:
        log.exception("agent tick failed")
        return

    log.info("\n%s", sig.explain())

    with SessionLocal() as db:
        db.add(SignalRecord(
            symbol=sig.symbol, timeframe=sig.timeframe, action=sig.action,
            confidence=sig.confidence, price=sig.price, entry=sig.entry,
            stop_loss=sig.stop_loss, target=sig.target,
            checks=[c.to_dict() for c in sig.checks], context=sig.context,
        ))
        db.commit()

    r = redis_client()
    if r:
        payload = sig.to_dict()
        payload["explanation"] = sig.explain()
        r.setex("signal:latest", 900, json.dumps(payload, default=str))
        r.publish("signals", json.dumps(payload, default=str))


def start() -> BackgroundScheduler:
    s = get_settings()
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(tick, "interval", minutes=s.agent_interval_minutes,
                      id="nifty-agent", max_instances=1, coalesce=True)
    scheduler.start()
    log.info("Nifty agent scheduled every %s minutes.", s.agent_interval_minutes)
    return scheduler
