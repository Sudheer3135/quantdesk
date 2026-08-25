"""What actually happened after each signal.

The desk has produced hundreds of signals and recorded one trade, so until
now there has been no way to answer the only question that matters about the
analysis: was it right? This reads stored signals against stored candles and
says what each one would have done.

Deliberately read-only, and deliberately not a strategy. Nothing here
computes a signal, adjusts a weight or changes a rule — it replays decisions
that were already made and already stored, using the backtest's own entry
and exit conventions so the answer is comparable with what the engine would
have produced.

Three things it is careful about:

  Look-ahead. Every price comes through `backtest.HistoricalFeed`, whose cursor only
  reveals bars the walk has reached. Evaluating a past signal against later
  bars is legitimate — both are history — but reading them through the same
  guard means the evaluation cannot accidentally use a bar before the moment
  it would have arrived.

  Echoes. The agent ran overnight for weeks, refiling the same closing
  candle's reading every five minutes. Those rows are excluded rather than
  deleted: the history stays intact and the filter is derived, so it
  self-corrects if the calendar is amended.

  Confidence. This measures whether confidence tracks outcomes. It does not
  assume it does, and the number is not a probability until something
  demonstrates it behaves like one.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import market_hours
from ..backtest.costs import CostModel, SlippageModel
from ..backtest.feed import HistoricalFeed
from ..data import repository
from ..models import SignalRecord

log = logging.getLogger(__name__)

# Matches `backtest.engine.run` so an outcome here means the same thing it
# would mean there. Changing either without the other makes the two
# incomparable, which is worse than either convention being wrong.
MAX_BARS_IN_TRADE = 24
SESSION_EXIT_HOUR, SESSION_EXIT_MINUTE = 15, 15

# One lot, always. Position size is a risk-manager decision that varies with
# capital and open positions; holding it fixed here keeps every signal
# measured on the same scale. R multiples are size-independent anyway and are
# the primary reading — the rupee figure exists to show costs biting.
EVALUATION_QUANTITY = 75

TARGET, STOP, SESSION_END, TIME_CAP, UNRESOLVED = (
    "target", "stop", "session_end", "time_cap", "unresolved")
RESOLVED = (TARGET, STOP, SESSION_END, TIME_CAP)

CONFIDENCE_BANDS: tuple[tuple[float, float], ...] = (
    (0.00, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01))


# ---------------------------------------------------------------------------
# which signals count
# ---------------------------------------------------------------------------

def is_in_session(moment: datetime) -> bool:
    """Was the exchange actually trading at this instant?

    `session_label` is the platform's own answer, holidays included. Reusing
    it means the evaluator cannot drift from what the dashboard says about
    the same moment.
    """
    return market_hours.session_label(moment) == market_hours.OPEN


def is_actionable(record: SignalRecord) -> bool:
    """A signal proposing a trade with all three levels."""
    return (record.action in ("BUY", "SELL")
            and record.entry is not None
            and record.stop_loss is not None
            and record.target is not None
            and abs(record.entry - record.stop_loss) > 0)


@dataclass
class SelectionReport:
    """Why the sample is the size it is. Every exclusion is counted, because
    a study that reports only what it kept is unauditable."""
    stored: int = 0
    holds: int = 0
    out_of_session: int = 0
    incomplete_levels: int = 0
    no_candle: int = 0
    selected: int = 0
    distinct_bars: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# one signal
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    signal_id: int
    signal_time: str
    action: str
    confidence: float
    trend: str | None

    # The bar the signal was computed on, as distinct from `signal_time`
    # (when the row was written) and `entry_time` (the next bar, where the
    # fill lands). Anything joining a per-bar series onto these outcomes —
    # the regime split — needs this exact bar, and re-deriving it from the
    # timestamp downstream would be a second implementation of the same
    # lookup, free to drift from this one.
    signal_bar_time: str
    entry_time: str
    entry: float
    stop: float
    target: float
    risk_per_unit: float

    outcome: str
    exit_time: str | None = None
    exit_price: float | None = None
    bars_held: int = 0
    minutes_held: float = 0.0

    # Excursions, in points and in units of the trade's own risk. The R
    # figures are what compare across signals with different stop widths.
    mfe_points: float = 0.0
    mae_points: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0

    # `r_multiple` is gross and price-based: (exit - entry) / risk. That is
    # the signal-quality reading, because it isolates whether the direction
    # and the levels were right from what it would have cost to trade them.
    gross_points: float | None = None
    r_multiple: float | None = None

    # Costs, kept but deliberately not the headline. The engine's model
    # computes turnover from the traded price, and at an index level of
    # 24,000 a 75-lot round trip is charged about 3,400 rupees — roughly
    # 2.3R at a twenty-point stop. Those are option-premium rates applied to
    # an index number, so `r_multiple_net` measures the mispricing far more
    # than it measures the signal. A real trade would pay them on a premium
    # of a hundred-odd rupees, not on the index.
    charges: float | None = None
    net_pnl: float | None = None
    r_multiple_net: float | None = None

    @property
    def resolved(self) -> bool:
        return self.outcome in RESOLVED

    @property
    def won(self) -> bool | None:
        """Did the signal work? Gross, for the reason given above.

        None while unresolved — an unfinished trade is not a loss.
        """
        if not self.resolved or self.r_multiple is None:
            return None
        return self.r_multiple > 0

    def to_dict(self) -> dict:
        return asdict(self) | {"resolved": self.resolved, "won": self.won}


def _resolve(feed: HistoricalFeed, entry_index: int, side: str, entry: float,
             stop: float, target: float) -> tuple[str, int | None]:
    """Walk forward until the trade ends. Returns (outcome, exit index).

    The exit rules are the engine's, including the pessimistic one: when a
    bar's range covers both the stop and the target, the stop is assumed to
    have filled first. Nothing in a 5-minute bar says which came first, and
    an evaluation that guessed favourably would flatter every signal whose
    bar happened to be wide.
    """
    last = len(feed) - 1
    for j in range(entry_index, last + 1):
        feed.seek(j)
        bar = feed.bar(j)
        moment = feed.ist(j)

        if side == "BUY":
            hit_stop = bar["low"] <= stop
            hit_target = bar["high"] >= target
        else:
            hit_stop = bar["high"] >= stop
            hit_target = bar["low"] <= target

        if hit_stop:
            return STOP, j
        if hit_target:
            return TARGET, j
        if j - entry_index >= MAX_BARS_IN_TRADE:
            return TIME_CAP, j
        if (moment.time().hour >= SESSION_EXIT_HOUR
                and moment.time().minute >= SESSION_EXIT_MINUTE):
            return SESSION_END, j

    # Ran out of stored candles. Not a loss and not a win: the archive
    # simply does not reach far enough yet, and counting it either way would
    # bias the study toward whichever way the last few days happened to go.
    return UNRESOLVED, None


def _excursions(feed: HistoricalFeed, entry_index: int, exit_index: int, side: str,
                entry: float) -> tuple[float, float]:
    """Best and worst the trade ever looked, in points."""
    window = feed._frame.iloc[entry_index : exit_index + 1]   # noqa: SLF001
    if window.empty:
        return 0.0, 0.0
    if side == "BUY":
        return (float(window["high"].max()) - entry,
                entry - float(window["low"].min()))
    return (entry - float(window["low"].min()),
            float(window["high"].max()) - entry)


def evaluate_signal(feed: HistoricalFeed, signal_index: int, record: SignalRecord,
                    costs: CostModel, slippage: SlippageModel,
                    quantity: int = EVALUATION_QUANTITY) -> Outcome:
    """Replay one stored signal against the bars that followed it.

    `signal_index` is the bar the signal was computed on. The fill is the
    next bar's open — the engine's convention, and the earliest price a
    decision made on a closed bar could actually have been given.
    """
    direction = 1 if record.action == "BUY" else -1

    feed.seek(signal_index)
    raw_open = feed.next_open(signal_index)
    entry_fill = raw_open + slippage.index_points(raw_open) * direction
    # The levels travel with the fill, exactly as the engine shifts them, so
    # a gap between the signal bar's close and the next open does not
    # silently widen or narrow the trade's risk.
    shift = entry_fill - record.entry
    stop = record.stop_loss + shift
    target = record.target + shift
    risk_per_unit = abs(entry_fill - stop)

    entry_index = signal_index + 1
    outcome_name, exit_index = _resolve(
        feed, entry_index, record.action, entry_fill, stop, target)

    out = Outcome(
        signal_id=record.id,
        signal_time=(repository.as_utc(record.created_at).isoformat()
                     if record.created_at else ""),
        action=record.action,
        confidence=float(record.confidence or 0.0),
        trend=(record.context or {}).get("trend"),
        signal_bar_time=feed.timestamp(signal_index).isoformat(),
        entry_time=feed.next_timestamp(signal_index).isoformat(),
        entry=round(entry_fill, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        risk_per_unit=round(risk_per_unit, 2),
        outcome=outcome_name,
    )

    if exit_index is None:
        feed.seek(len(feed) - 1)
        out.mfe_points, out.mae_points = _excursions(
            feed, entry_index, len(feed) - 1, record.action, entry_fill)
    else:
        feed.seek(exit_index)
        bar = feed.bar(exit_index)
        exit_price = {STOP: stop, TARGET: target}.get(
            outcome_name, float(bar["close"]))

        slip = slippage.index_points(exit_price) * direction
        fill = exit_price - slip
        gross = (fill - entry_fill) * direction * quantity
        charges = costs.round_trip(
            buy_price=min(entry_fill, fill),
            sell_price=max(entry_fill, fill),
            quantity=quantity)

        out.exit_time = feed.timestamp(exit_index).isoformat()
        out.exit_price = round(fill, 2)
        out.bars_held = exit_index - entry_index + 1
        out.minutes_held = round(
            (feed.timestamp(exit_index) - feed.timestamp(entry_index))
            .total_seconds() / 60, 1)
        out.gross_points = round((fill - entry_fill) * direction, 2)
        out.r_multiple = (round(out.gross_points / risk_per_unit, 3)
                          if risk_per_unit else 0.0)
        out.charges = round(charges.total, 2)
        out.net_pnl = round(gross - charges.total, 2)
        out.r_multiple_net = (round(out.net_pnl / (risk_per_unit * quantity), 3)
                              if risk_per_unit else 0.0)
        out.mfe_points, out.mae_points = _excursions(
            feed, entry_index, exit_index, record.action, entry_fill)

    if risk_per_unit:
        out.mfe_r = round(out.mfe_points / risk_per_unit, 3)
        out.mae_r = round(out.mae_points / risk_per_unit, 3)
    out.mfe_points = round(out.mfe_points, 2)
    out.mae_points = round(out.mae_points, 2)
    return out


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

@dataclass
class Bucket:
    """One slice of the sample. `n` first, because every figure under it is
    meaningless without it."""
    label: str
    n: int = 0
    resolved: int = 0
    target_first: int = 0
    stop_first: int = 0
    session_end: int = 0
    time_cap: int = 0
    unresolved: int = 0
    wins: int = 0
    win_rate: float | None = None
    avg_r: float | None = None           # gross, the signal-quality reading
    total_r: float | None = None
    avg_r_net: float | None = None       # after the engine's cost model
    avg_net_pnl: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    median_minutes: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def bucket(label: str, rows: Sequence[Outcome]) -> Bucket:
    b = Bucket(label=label, n=len(rows))
    for row in rows:
        if row.outcome == TARGET:
            b.target_first += 1
        elif row.outcome == STOP:
            b.stop_first += 1
        elif row.outcome == SESSION_END:
            b.session_end += 1
        elif row.outcome == TIME_CAP:
            b.time_cap += 1
        else:
            b.unresolved += 1

    done = [r for r in rows if r.resolved and r.r_multiple is not None]
    b.resolved = len(done)
    if not done:
        return b

    b.wins = sum(1 for r in done if r.won)
    b.win_rate = round(b.wins / len(done), 3)
    rs = [r.r_multiple for r in done]
    b.avg_r = round(float(np.mean(rs)), 3)
    b.total_r = round(float(np.sum(rs)), 3)
    b.avg_r_net = round(float(np.mean([r.r_multiple_net for r in done])), 3)
    b.avg_net_pnl = round(float(np.mean([r.net_pnl for r in done])), 2)
    b.avg_mfe_r = round(float(np.mean([r.mfe_r for r in done])), 3)
    b.avg_mae_r = round(float(np.mean([r.mae_r for r in done])), 3)
    b.median_minutes = round(float(np.median([r.minutes_held for r in done])), 1)
    return b


# The previous private name, kept so nothing in-module has to change.
_bucket = bucket


def _confidence_label(value: float) -> str:
    for low, high in CONFIDENCE_BANDS:
        if low <= value < high:
            return f"{low:.2f}–{high:.2f}"
    return "unbanded"


def _hour_label(row: Outcome) -> str:
    stamp = pd.Timestamp(row.entry_time).tz_convert("Asia/Kolkata")
    return f"{stamp.hour:02d}:00–{stamp.hour:02d}:59"


def group_by(rows: Sequence[Outcome], key) -> list[dict]:
    """Split outcomes by any key and bucket each group.

    Public so that a study slicing these outcomes a different way — by
    regime, say — gets statistics defined by this function rather than by a
    second implementation of "win rate" that is free to disagree with it.
    """
    buckets: dict[str, list[Outcome]] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    return [_bucket(label, group).to_dict()
            for label, group in sorted(buckets.items())]


# The previous private name, kept so nothing in-module has to change.
_group = group_by


def _spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Rank correlation, computed here rather than pulling in scipy.

    Rank-based on purpose: the question is whether *more* confidence goes
    with *better* outcomes, not whether the relationship is linear. A single
    huge R multiple should not decide the answer.
    """
    if len(a) < 3:
        return None
    ranked_a = pd.Series(a).rank().to_numpy()
    ranked_b = pd.Series(b).rank().to_numpy()
    if ranked_a.std() == 0 or ranked_b.std() == 0:
        return None
    return round(float(np.corrcoef(ranked_a, ranked_b)[0, 1]), 3)


def confidence_relationship(rows: Sequence[Outcome]) -> dict:
    """Does confidence track outcomes?

    Reported as a measurement, never as a claim. A confidence score is only
    a probability if something demonstrates it behaves like one, and nothing
    in this codebase has demonstrated that. Until the correlation is both
    positive and supported by a sample worth the name, the honest reading is
    that the number orders signals no better than chance.
    """
    # Stated whatever the sample size. This sentence matters *most* when
    # there is too little data to test it, because that is exactly when a
    # confidence number invites being read as a probability.
    interpretation = (
        "Confidence is not calibrated and must not be read as a "
        "probability. What is reported here is the observed rank "
        "correlation between the confidence attached to a signal and what "
        "the signal went on to do; it says whether the number orders "
        "signals usefully, nothing more.")

    done = [r for r in rows if r.resolved and r.r_multiple is not None]
    if len(done) < 3:
        return {
            "measurable": False,
            "reason": f"only {len(done)} resolved signal(s); too few to say anything",
            "resolved": len(done),
            "interpretation": interpretation,
        }

    conf = [r.confidence for r in done]
    return {
        "measurable": True,
        "resolved": len(done),
        "spearman_confidence_vs_r": _spearman(conf, [r.r_multiple for r in done]),
        "spearman_confidence_vs_win": _spearman(
            conf, [1.0 if r.won else 0.0 for r in done]),
        "distinct_confidence_values": len(set(conf)),
        "interpretation": interpretation,
    }


# ---------------------------------------------------------------------------
# the study
# ---------------------------------------------------------------------------

@dataclass
class EvaluationReport:
    symbol: str
    timeframe: str
    selection: dict
    candles: dict
    overall: dict
    by_confidence: list[dict]
    by_direction: list[dict]
    by_hour: list[dict]
    # The signal engine's own `context["trend"]` label — a read of price
    # structure at the moment the signal fired, and never a market regime,
    # though it was called `by_regime` here until one existed. The real
    # regime split lives in `evaluation.regime_report`, which classifies
    # volatility, efficiency, VWAP behaviour and the session's shape. Two
    # different things under one name in the same report would have been
    # read as one.
    by_trend_label: list[dict]
    confidence: dict
    outcomes: list[dict]
    caveats: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


CAVEATS = [
    "Hypothetical. No order was placed; these are stored signals replayed "
    "against stored candles.",
    "Observations are not independent. The agent emits a signal every five "
    "minutes, so consecutive signals in one move describe the same move and "
    "a win rate over them overstates how much evidence there is.",
    "When one bar's range covers both the stop and the target, the stop is "
    "assumed to have filled first. Five-minute bars do not record which came "
    "first, and the pessimistic reading is the only safe one.",
    "Headline R multiples are GROSS. The engine's cost model computes "
    "turnover from the traded price, so at an index level of 24,000 it "
    "charges about 3,400 rupees a round trip — roughly 2.3R at a "
    "twenty-point stop. Those are option-premium rates applied to an index "
    "number. `avg_r_net` is reported for completeness and mostly measures "
    "that mismatch; a real trade would pay charges on a premium of a "
    "hundred-odd rupees. Read gross for signal quality, and do not read net "
    "as expectancy.",
    "Unresolved signals are excluded from win rates rather than counted as "
    "losses — the archive simply does not reach past them yet.",
]


@dataclass
class Selection:
    """The sample, and the audit trail of how it was chosen.

    Split out of `evaluate` so a study that slices these outcomes a
    different way does not have to re-select them. Re-running the selection
    is not merely wasteful — two copies of "which signals count" would be
    free to drift, and the regime split would then be measuring a different
    167 signals from the headline study.
    """
    outcomes: list[Outcome] = field(default_factory=list)
    report: SelectionReport = field(default_factory=SelectionReport)
    candles: pd.DataFrame = field(default_factory=pd.DataFrame)


def collect(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
            cost_model: CostModel | None = None,
            slippage_model: SlippageModel | None = None,
            quantity: int = EVALUATION_QUANTITY) -> Selection:
    """Every genuine in-session signal, replayed against what happened next."""
    costs = cost_model or CostModel()
    slippage = slippage_model or SlippageModel()

    records = list(db.scalars(
        select(SignalRecord)
        .where(SignalRecord.symbol == symbol, SignalRecord.timeframe == timeframe)
        .order_by(SignalRecord.created_at)
    ).all())

    selection = SelectionReport(stored=len(records))
    candles = repository.load_index_candles(db, symbol, timeframe)

    if candles.empty or not records:
        return Selection(outcomes=[], report=selection, candles=candles)

    feed = HistoricalFeed(candles)
    stamps = feed._frame["timestamp"]                       # noqa: SLF001

    outcomes: list[Outcome] = []
    seen_bars: set[int] = set()

    for record in records:
        if not is_actionable(record):
            if record.action == "HOLD":
                selection.holds += 1
            else:
                selection.incomplete_levels += 1
            continue
        if record.created_at is None:
            selection.out_of_session += 1
            continue

        # Normalised before anything reads it. SQLite hands `created_at`
        # back naive while Postgres returns it aware, and `to_ist` treats a
        # naive value as *already IST* — so a 09:15 signal stored as 03:45
        # UTC came back as 03:45 and read as the middle of the night. The
        # study then silently excluded every in-session signal on one
        # backend and none on the other. `repository.as_utc` is the same
        # normalisation `todays_trades` uses for the same reason.
        stamped = repository.as_utc(record.created_at)
        if not is_in_session(stamped):
            selection.out_of_session += 1
            continue

        # The bar the signal was computed on: the last one that had closed.
        # `searchsorted` on the right, minus one, is that bar.
        position = int(stamps.searchsorted(
            pd.Timestamp(stamped), side="right")) - 1
        if position < 0 or position >= len(feed) - 1:
            # Either the archive does not reach back to this signal, or the
            # signal sits on the final stored bar and could never have been
            # filled. Both are missing data, not outcomes.
            selection.no_candle += 1
            continue

        seen_bars.add(position)
        outcomes.append(evaluate_signal(
            feed, position, record, costs, slippage, quantity))

    selection.selected = len(outcomes)
    selection.distinct_bars = len(seen_bars)
    return Selection(outcomes=outcomes, report=selection, candles=candles)


def candle_span(candles: pd.DataFrame) -> dict:
    """What the archive covers, for the header of any study built on it."""
    if candles.empty:
        return {"rows": 0}
    stamps = pd.to_datetime(candles["timestamp"], utc=True)
    return {"rows": int(len(candles)),
            "first": stamps.iloc[0].isoformat(),
            "last": stamps.iloc[-1].isoformat()}


def evaluate(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
             cost_model: CostModel | None = None,
             slippage_model: SlippageModel | None = None,
             quantity: int = EVALUATION_QUANTITY,
             include_outcomes: bool = True) -> EvaluationReport:
    """Replay every genuine in-session signal against what happened next."""
    picked = collect(db, symbol, timeframe, cost_model, slippage_model, quantity)
    outcomes = picked.outcomes

    return EvaluationReport(
        symbol=symbol, timeframe=timeframe,
        selection=picked.report.to_dict(),
        candles=candle_span(picked.candles),
        overall=_bucket("all", outcomes).to_dict(),
        by_confidence=group_by(outcomes, lambda r: _confidence_label(r.confidence)),
        by_direction=group_by(outcomes, lambda r: r.action),
        by_hour=group_by(outcomes, _hour_label),
        by_trend_label=group_by(outcomes, lambda r: r.trend or "unknown"),
        confidence=confidence_relationship(outcomes),
        outcomes=[o.to_dict() for o in outcomes] if include_outcomes else [],
        caveats=CAVEATS,
    )
