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
from ..cache import client as redis_client
from ..config import get_settings
from ..data import importer
from ..db import SessionLocal
from ..deps import get_broker
from ..market_hours import is_open as market_is_open
from ..models import SignalRecord

log = logging.getLogger(__name__)


def _capture_option_chain(db, settings) -> None:
    """Store one option-chain snapshot.

    This is the only mechanism by which option history comes to exist. NSE
    publishes a live snapshot rather than a tape, and nobody sells the
    history at a price a retail account would pay — so a bar not captured
    now is a bar that can never be recovered. That asymmetry is the reason
    this runs on every tick.

    It refuses to file a snapshot whose expiry it cannot name. Guessing
    would merge two different contracts into one series, and the resulting
    premium history would look perfectly plausible.
    """
    broker = get_broker()
    if not hasattr(broker, "chain_with_spot"):
        return

    chain, spot = broker.chain_with_spot(settings.watch_symbol)
    expiry = chain.attrs.get("expiry")
    if not expiry:
        log.debug("chain carried no expiry label; skipping snapshot")
        return

    importer.import_option_snapshot(
        db, chain, underlying=settings.watch_symbol, expiry=expiry,
        spot=spot, source=settings.broker,
        timeframe=settings.watch_timeframe, lot_size=settings.lot_size)


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

    if s.archive_option_chain and market_is_open():
        try:
            with SessionLocal() as db:
                _capture_option_chain(db, s)
        except Exception:
            log.exception("option chain snapshot failed")

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
