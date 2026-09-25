import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import options as option_analytics
from ..analytics import plan as plan_builder
from ..analytics import decision_provenance, indicators, signal_engine, warmup
import pandas as pd
from ..brokers.base import UnknownSymbol
from ..db import get_db
from ..deps import get_broker
from ..evaluation import outcomes as outcome_study
from ..evaluation import regime_report, two_layer
from ..models import SignalRecord
from ..risk import live as risk_live
from ..security import HEADER as API_KEY_HEADER
from ..security import key_is_valid
from ..symbols import validate as validate_symbol

log = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


@dataclass
class Analysis:
    """One market read: the existing verdict, and the two-layer plan beside it.

    Both are built from the same candle frame and the same option chain in
    one pass. Fetching twice would let the signal and the plan describe
    different bars — a five-minute boundary crossing between two broker calls
    is enough — and the dashboard would show a BUY against a plan formed on a
    different price.
    """
    signal: signal_engine.Signal
    plan: plan_builder.Plan | None = None


def build_analysis(symbol: str, timeframe: str, days: int = 5) -> Analysis:
    """The signal and the plan, from one fetch.

    The plan is additive: it changes nothing about how the signal is reached.
    If building it fails the signal still ships, because a missing plan costs
    the desk a caption and a missing signal costs it the screen.
    """
    symbol = validate_symbol(symbol)
    broker = get_broker()
    candles = broker.candles(symbol, timeframe, days)
    # When Quant Desk actually received the candles — the moment the bars
    # became available to it, never inferred from the exchange's clock. A
    # bar is complete in *this copy* only if it had closed before the copy
    # was taken, so completeness is judged at receipt: judging it at the
    # later decision instant would admit a bar that was fetched while still
    # forming and closed only afterwards.
    received_at = pd.Timestamp.now(tz="UTC")
    candles = indicators.drop_unclosed(candles, timeframe, as_of=received_at)
    candles.attrs["received_at"] = received_at.isoformat()
    decision_time = pd.Timestamp.now(tz="UTC")
    candles.attrs["decision_time"] = decision_time.isoformat()
    try:
        chain = broker.option_chain(symbol)
    except Exception:
        chain = None

    vix = broker.india_vix()
    decision_time = pd.Timestamp.now(tz="UTC")
    candles = indicators.drop_unclosed(candles, timeframe, as_of=decision_time)
    # The same declared history a replay of this bar sees (TC-5). Five days
    # of candles is ~375 bars; the backtest reads 300; the EMA200 of the two
    # differ, so the live signal used to be computed on different inputs
    # from any replay of it.
    candles = warmup.declared_history(candles)
    candles.attrs["decision_time"] = decision_time.isoformat()
    if chain is not None:
        source_time = chain.attrs.get("source_time")
        try:
            source_time = pd.Timestamp(source_time) if source_time else None
            if (source_time is None or source_time.tzinfo is None
                    or source_time > decision_time
                    or decision_time - source_time > pd.Timedelta(minutes=5)):
                chain = None
        except (ValueError, TypeError):
            chain = None
    signal = signal_engine.generate(
        candles, symbol=symbol, timeframe=timeframe,
        chain=chain, india_vix=vix,
    )

    plan = None
    try:
        # The chain summary the engine already computed, reused rather than
        # recomputed — two summaries of one chain is two chances to disagree
        # about what the positioning says.
        summary = None
        stored = (signal.context or {}).get("option_chain")
        if stored:
            summary = option_analytics.ChainSummary(**stored)
        plan = plan_builder.build(candles, symbol=symbol, timeframe=timeframe,
                                  chain=chain, chain_summary=summary)
    except Exception:
        log.exception("plan build failed; serving the signal without it")

    return Analysis(signal=signal, plan=plan)


def build_signal(symbol: str, timeframe: str, days: int = 5) -> signal_engine.Signal:
    """The verdict alone. Kept because plenty of callers only want that."""
    return build_analysis(symbol, timeframe, days).signal


def plan_columns(built: plan_builder.Plan | None) -> dict:
    """The signal row's plan fields. One definition, used by both writers.

    The agent and `/signals/live` both persist signals, and the two-layer
    read has to land in the same three columns from either. The last time a
    field was assembled separately in these two routes, one of them shipped
    without it for weeks — see audit finding H-4.
    """
    if built is None:
        return {"bias": None, "entry_state": None, "plan": None}
    return {"bias": built.bias["label"],
            "entry_state": built.entry["state"],
            "plan": built.to_dict()}


def provenance_columns(sig: signal_engine.Signal) -> dict:
    """The signal row's clock and provenance columns (TC-2, RP-2).

    One definition for both writers, like `plan_columns` above and for the
    same reason. The code identifier is read here — once per stored signal,
    not per bar inside the engine — and names a dirty tree as dirty.
    """
    from ..backtest import measurement
    from ..config import get_settings

    return decision_provenance.columns(
        sig, data_source=get_settings().broker, code_id=measurement.code_id())


def note_exposure(db: Session, sig, channel: str) -> None:
    """Serving or storing a live signal shows the strategy that session. If
    the session is protected prospective holdout data, it is seen from now
    on (Pass 2D, `methodology.registry`)."""
    from ..methodology import registry

    timing = (sig.context or {}).get("timing") or {}
    moment = timing.get("decision_at") or timing.get("signal_time")
    moment = pd.Timestamp(moment).to_pydatetime() if moment else \
        pd.Timestamp.now(tz="UTC").to_pydatetime()
    registry.note_strategy_output(db, moment=moment, channel=channel,
                                  detail=f"{sig.symbol} {sig.timeframe} {sig.action}")


@router.get("/live")
def live_signal(symbol: str = "NIFTY", timeframe: str = "5m",
                persist: bool = False, db: Session = Depends(get_db),
                x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)):
    # Reading a signal is open; asking for it to be *stored* is not. This is
    # a GET that writes a row, so it needs the same key as the POST routes —
    # guarding by HTTP verb alone would have left this one open.
    if persist and not key_is_valid(x_api_key):
        raise HTTPException(
            401, f"persist=true writes a signal row. Send your key in the {API_KEY_HEADER} header.")
    try:
        analysis = build_analysis(symbol, timeframe)
        sig = analysis.signal
    except UnknownSymbol:
        # The caller named something we do not carry. Let it reach the 422
        # handler instead of being relabelled as an upstream failure.
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    payload = sig.to_dict()
    payload["explanation"] = sig.explain()
    payload["plan"] = analysis.plan.to_dict() if analysis.plan else None

    # The risk decision is assembled in `risk.live` and nowhere else. It used
    # to be built inline here, which is how the agent's route — the one the
    # dashboard actually reads — ended up publishing signals with no risk
    # block at all. See audit finding H-4.
    risk_live.attach(db, payload, sig)
    note_exposure(db, sig, "strategy_signal:/signals/live")

    if persist:
        record = SignalRecord(
            symbol=sig.symbol, timeframe=sig.timeframe, action=sig.action,
            confidence=sig.confidence, price=sig.price, entry=sig.entry,
            stop_loss=sig.stop_loss, target=sig.target,
            checks=[c.to_dict() for c in sig.checks], context=sig.context,
            # Stored so "what did the desk decide about this signal, and
            # why" is answerable from the database rather than only from
            # whatever was on screen at the time.
            risk=payload["risk"],
            **plan_columns(analysis.plan),
            **provenance_columns(sig),
        )
        db.add(record)
        db.commit()
        payload["id"] = record.id

    return payload


@router.get("/history")
def signal_history(limit: int = 50, db: Session = Depends(get_db)):
    """The signal journal, as the desk recorded it.

    The two-layer read and the risk verdict are included because a feed row
    without them cannot be read: an action alone does not say which direction
    the higher timeframe pointed, whether this was the moment, or whether the
    desk would have been allowed to take it. All four already live on the row
    — this only stops discarding them on the way out.

    Additive: every field the previous response carried is still here and
    still spelled the same, so an existing caller sees no change.
    """
    rows = db.scalars(
        select(SignalRecord).order_by(SignalRecord.created_at.desc()).limit(limit)
    ).all()
    return [
        {"id": r.id, "created_at": r.created_at, "symbol": r.symbol, "action": r.action,
         "confidence": r.confidence, "price": r.price, "entry": r.entry,
         "stop_loss": r.stop_loss, "target": r.target,
         "bias": r.bias, "entry_state": r.entry_state,
         # The regime the plan was formed in, if the plan recorded one. Read
         # from the stored plan rather than re-derived, so the row says what
         # the desk actually saw and never a reconstruction of it.
         "regime_day": ((r.plan or {}).get("entry") or {}).get("regime_day"),
         "regime_hour": ((r.plan or {}).get("entry") or {}).get("regime_hour"),
         # The verdict only. The reasons are long and belong to the detail
         # view; a feed needs to show approved-or-not at a glance.
         "risk_state": (r.risk or {}).get("state"),
         }
        for r in rows
    ]


@router.get("/outcomes")
def signal_outcomes(symbol: str = "NIFTY", timeframe: str = "5m",
                    include_signals: bool = False,
                    db: Session = Depends(get_db)):
    """What actually happened after each stored signal.

    Read-only, and it computes no signal of its own: it replays decisions
    already on record against candles already on record, using the backtest's
    entry and exit conventions so the answer is comparable with what the
    engine would have produced.

    `include_signals=true` returns every individual outcome. The default is
    the summary, because the per-signal list is long and the aggregate is
    what answers the question.

    Read `selection` before anything else. It says how many stored signals
    were excluded and why, and a win rate whose sample you have not looked at
    is not a result.
    """
    symbol = validate_symbol(symbol)
    # Kept until a signal or a candle lands. A pure replay of stored rows
    # against stored rows, recomputed every minute by an open dashboard —
    # see `report_cache` for why that is safe and what bounds it.
    from ..data import report_cache
    return report_cache.memoise(
        ("outcomes", symbol, timeframe, include_signals), db,
        lambda: outcome_study.evaluate(
            db, symbol, timeframe, include_outcomes=include_signals).to_dict())


@router.get("/outcomes/by-regime")
def outcomes_by_regime(symbol: str = "NIFTY", timeframe: str = "5m",
                       db: Session = Depends(get_db)):
    """The same evaluated signals, split by the market condition they fired in.

    The headline outcome study averages over every condition the market can
    be in, which is an average of things that should not be added together:
    a rule that works in a trend and fails in chop looks like a rule that
    does not work. This asks where it fails.

    Read `matched` and `unmatched` first. Unmatched signals are reported as
    an `unclassified` bucket rather than dropped — quietly shrinking the
    sample would let a split of 140 be compared against a headline of 167
    without anyone noticing. If `unmatched` is large, the regime table is
    behind the candle archive: POST /data/regimes/backfill.

    Every bucket carries an `interpretation` saying whether its `n` is large
    enough to mean anything. Most are not, yet.
    """
    symbol = validate_symbol(symbol)
    return regime_report.build(db, symbol, timeframe).to_dict()


@router.get("/outcomes/two-layer")
def outcomes_two_layer(symbol: str = "NIFTY", timeframe: str = "5m",
                       include_rows: bool = False,
                       db: Session = Depends(get_db)):
    """The evaluated signals seen through the bias and entry-state layers.

    Answers the two questions separately, because they fail separately. A
    bias can be right while every trade taken on it is stopped out — that is
    exactly the case Step 1 found, where eleven with-trend signals had an
    average best moment of 0.059R.

    Read in this order:

      `bias_accuracy.edge_over_base_rate` — not `accuracy`. If most hours in
      the sample closed up, a permanently bullish model scores well and
      knows nothing.

      `entry_states` — what the model WOULD have said. Nothing was gated on
      it; that is a later step, and this is the evidence for it.

      `reinterpretation` — the two groups side by side. Suggestive at best:
      167 non-independent observations over thirteen trading days.

    The plans are recomputed from the archive at each signal's own bar, from
    a window the size the live path actually sees. Nothing is written back.
    """
    symbol = validate_symbol(symbol)
    return two_layer.build(db, symbol, timeframe,
                           include_rows=include_rows).to_dict()
