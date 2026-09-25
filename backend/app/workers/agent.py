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

from .. import net
from ..api.signals import build_analysis, plan_columns, provenance_columns
from ..cache import publish
from ..config import get_settings
from ..data import importer, regime_store
from ..db import SessionLocal
from ..deps import get_broker
from ..market_hours import is_open as market_is_open
from ..models import SignalRecord
from ..risk import live as risk_live

log = logging.getLogger(__name__)


def tick(force: bool = False) -> None:
    """One scheduled pass: archive, analyse, decide, publish.

    Gated on the session in every environment, not only in production.

    Development used to run this around the clock, and the signal table
    shows what that produced: of 375 stored BUY signals, 320 were generated
    outside market hours. The agent was recomputing the same closing candle
    every five minutes all night and filing the answer again each time, so
    half the table is one reading repeated rather than a series of
    observations. Anything that counts those rows — an outcome study most of
    all — is counting an echo.

    `force` runs a pass regardless, for diagnostics and for tests that are
    about what a tick produces rather than about when it fires. The option
    collector's `capture` takes the same escape hatch for the same reason.
    """
    s = get_settings()
    if not force and not market_is_open():
        log.debug("market shut — no signal this tick")
        return

    # Every outbound call this tick makes shares one budget, sized from the
    # agent's own interval. Without it a hung candle or chain request runs
    # past the next scheduled run, `max_instances=1` skips it, and the desk
    # stops producing signals while the process looks entirely healthy —
    # observed on 24 and 25-Aug-2026, 37 signals against 76 bars.
    with net.budget(net.budget_for(s.agent_interval_minutes * 60),
                    label="nifty-agent"):
        _analyse(s)


def _analyse(s) -> None:
    """The body of a tick, inside the caller's time budget."""
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
        except net.BudgetExhausted as exc:
            log.warning("candle archiving gave up to keep the schedule: %s", exc)
        except Exception:
            log.exception("candle archiving failed")

        # Classify the bars that just arrived. Kept next to the archiving
        # step because it is the same kind of work — recording what happened
        # — and because a regime table that only advances when someone runs a
        # backfill by hand is a research table, not a live one.
        #
        # Guarded separately from the import above: a classifier failure must
        # not cost the desk its candles, which are the part that cannot be
        # re-fetched later on a free source.
        try:
            with SessionLocal() as db:
                regime_store.refresh_recent(db, s.watch_symbol, s.watch_timeframe)
        except Exception:
            log.exception("regime classification failed")

    # The option chain is captured by `workers.option_collector`, not here.
    # It has to poll faster than the bar width to build a bar with a real
    # range, and the agent's cadence is the strategy's timeframe and should
    # not be driven by a data-collection need.

    try:
        analysis = build_analysis(s.watch_symbol, s.watch_timeframe)
        sig = analysis.signal
    except net.BudgetExhausted as exc:
        # Giving the slot back is the point. The next tick starts with a
        # full budget instead of queueing behind this one.
        log.error("agent tick gave up to keep the schedule: %s", exc)
        return
    except Exception:
        log.exception("agent tick failed")
        return

    log.info("\n%s", sig.explain())
    if analysis.plan:
        log.info("plan: %s", analysis.plan.explain())

    payload = sig.to_dict()
    payload["explanation"] = sig.explain()
    # The two-layer read rides with the signal rather than on its own channel.
    # Both describe the same bar, and a dashboard that received them
    # separately could render a BUY beside a plan formed on a different price.
    payload["plan"] = analysis.plan.to_dict() if analysis.plan else None

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
            **plan_columns(analysis.plan),
            **provenance_columns(sig),
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
