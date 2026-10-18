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
from ..analytics import decision_provenance
from ..backtest import execution
from ..backtest.costs import CostModel, FlatCostModel, SlippageModel
from ..backtest.execution import ExecutionPolicy
from ..backtest.feed import HistoricalFeed
from ..data import repository, research, schema
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
# A stop or target the bar gapped straight through is still a stop or a
# target for the purpose of counting outcomes — the difference is the price
# it filled at, which is carried separately. Folding them into one bucket
# here keeps `by_confidence` and friends comparable with the study as it was
# before; the gap is visible per outcome and counted in the overall bucket.
GAPPED = {execution.STOP_GAP: STOP, execution.TARGET_GAP: TARGET,
          execution.STOP: STOP, execution.TARGET: TARGET}
RESOLVED = (TARGET, STOP, SESSION_END, TIME_CAP)

# The evaluator trades the index, not a premium, so it charges the index
# engine's flat figure. Charging the option schedule against an index level
# — 24,000 a point instead of a hundred-odd rupees of premium — billed
# about 3,400 rupees a round trip, roughly 2.3R at a twenty-point stop, and
# turned every net figure in this study into a measurement of that mistake
# rather than of the signals. Same default as `backtest.engine.run`, so the
# evaluator and the engine now price the same trade the same way.
EVALUATION_COST_PER_ROUND_TRIP = 120.0

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
    duplicate_bars: int = 0
    invalid_timing: int = 0
    legacy_inferred_timing: int = 0

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
    timing_basis: str = "recorded_bar_time"
    signal_bar_close_time: str | None = None
    earliest_execution_time: str | None = None
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

    # The execution record, identical in meaning to the engines'. Without
    # these the study said a trade "stopped" and gave a price, and there was
    # no way to tell from the row whether that price had ever existed.
    entry_side: str = ""
    exit_side: str = ""
    planned_entry: float = 0.0
    actual_entry: float = 0.0
    planned_stop: float = 0.0
    planned_target: float = 0.0
    gap_amount: float = 0.0
    execution_policy: str = execution.KEEP_PLANNED
    # What the bar actually did, as distinct from `outcome`, which buckets
    # a gapped stop with an ordinary one.
    exit_basis: str = ""
    ambiguous_intrabar: bool = False
    gross_pnl: float | None = None
    execution_friction: float | None = None
    brokerage: float | None = None
    statutory_fees: float | None = None
    actual_fill_time: str | None = None

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
             stop: float, target: float,
             policy: ExecutionPolicy = execution.DEFAULT_POLICY
             ) -> tuple[str, int | None, execution.LevelExit | None]:
    """Walk forward until the trade ends.

    Returns (outcome, exit index, the level exit if a level caused it).

    The exit rules are the engine's — literally the engine's now, through
    `execution.resolve_levels`, rather than a second implementation that
    agreed with it in prose. The difference that mattered: this function
    used to report *which* level was hit and let the caller price the exit
    at that level, so a long stopped at 99 on a bar that opened at 97 was
    recorded as exiting at 99. The engine had always taken the 97. The
    evaluator's numbers were therefore better than the engine's on exactly
    the trades where the market moved fastest, and the dashboard showed
    fills at prices that never traded.

    The pessimistic rule is unchanged and now counted: when a bar covers
    both levels and its open settles neither, the stop is assumed and the
    trade is marked ambiguous.
    """
    last = len(feed) - 1
    for j in range(entry_index, last + 1):
        feed.seek(j)
        bar = feed.bar(j)
        moment = feed.ist(j)

        hit = execution.resolve_levels(
            side, bar_open=float(bar["open"]), high=float(bar["high"]),
            low=float(bar["low"]), stop=stop, target=target, policy=policy)
        if hit is not None:
            return GAPPED[hit.reason], j, hit
        if j - entry_index >= MAX_BARS_IN_TRADE:
            return TIME_CAP, j, None
        if (moment.time().hour >= SESSION_EXIT_HOUR
                and moment.time().minute >= SESSION_EXIT_MINUTE):
            return SESSION_END, j, None

    # Ran out of stored candles. Not a loss and not a win: the archive
    # simply does not reach far enough yet, and counting it either way would
    # bias the study toward whichever way the last few days happened to go.
    return UNRESOLVED, None, None


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


class NoExecutableBar(ValueError):
    """The archive holds no bar this decision could legally have filled on.

    Distinct from "the trade never resolved": nothing was ever entered, so
    there is no outcome to report. Raised rather than silently filled at the
    next bar, which is what used to happen and is how a latency became
    decorative.
    """


PERSISTED_CLOCK = "persisted_clock"


def evaluate_signal(feed: HistoricalFeed, signal_index: int, record: SignalRecord,
                    costs: CostModel, slippage: SlippageModel,
                    quantity: int = EVALUATION_QUANTITY, *, execution_index: int | None = None,
                    timing_basis: str = "recorded_bar_time",
                    policy: ExecutionPolicy | None = None,
                    decision_time=None, execution_floor=None) -> Outcome:
    """Replay one stored signal against the bars that followed it.

    `signal_index` is the bar the signal was computed on. The fill is the
    open of the first bar the order could have reached — the engine's
    convention, latency included, and the earliest price a decision made on
    a closed bar could actually have been given.

    The execution clock is enforced *here*, not by whoever calls this. It
    used to be the other way round: the fill bar defaulted to
    `signal_index + 1` and only `collect` did the latency arithmetic, so a
    direct call with a one-second latency — or a 299-, 300- or 301-second
    one — filled at the very next open regardless, and the
    `earliest_execution_time` recorded on the row was a number nothing had
    checked the fill against. A latency that only a caller honours is not a
    latency.

    `decision_time` is the recorded moment the signal was made, when the
    caller has one. It is the true start of the execution clock; without it
    the clock starts at the close of the bar the signal was computed on,
    which is the earliest the decision could possibly have existed.

    Persisted clocks are enforced here, from the record itself (TC-2). A
    row whose `clock_basis` is persisted is validated by
    `decision_provenance.persisted` — the same check `collect` uses — before
    any price is read, and fails closed if a clock is missing or out of
    order. Its deadline is then

        max(persisted earliest_execution_time,
            persisted decision_at + policy latency,
            execution_floor, if the caller gave one)

    so a caller can make execution later and never earlier. It used to be
    the caller's job to pass the persisted deadline in; a direct call that
    left `execution_floor` out filled at whatever bar it named.
    `execution_floor` earlier than the decision is refused as malformed.
    Only a genuinely legacy row may be evaluated without persisted clocks,
    and it may not be labelled `persisted_clock`.

    Returns an Outcome whose `outcome` is `entry_rejected_due_to_gap` when
    the fill invalidated the trade before it began. Such a row is not
    resolved, never carries an exit, and cannot be counted as a win or a
    loss — it is a trade that did not happen.
    """
    policy = policy or ExecutionPolicy()
    direction = 1 if record.action == "BUY" else -1

    # 1. The record's own clocks, checked before anything else is touched.
    clock = None
    if decision_provenance.claims_persisted(record):
        clock = decision_provenance.persisted(record, timeframe_minutes=5)
        timing_basis = PERSISTED_CLOCK
    elif timing_basis == PERSISTED_CLOCK:
        raise decision_provenance.ClockViolation(
            f"signal {record.id} has no persisted clocks and cannot be "
            "evaluated as persisted_clock")

    feed.seek(signal_index)
    stamps = feed.stamps()
    if clock is not None and stamps.iloc[signal_index] != clock["bar_open_time"]:
        raise decision_provenance.ClockViolation(
            f"signal bar {stamps.iloc[signal_index].isoformat()} is not the "
            f"persisted bar {clock['bar_open_time'].isoformat()}")
    signal_bar_close = stamps.iloc[signal_index] + pd.Timedelta(minutes=5)
    # A decision cannot predate the close of the bar it was computed on, so
    # the bar close is the floor on the execution clock even when a recorded
    # stamp claims otherwise. Where the recorded stamp is later — the agent
    # wrote the row some seconds after the bar closed — that later moment is
    # the truer start, and using the bar close instead would grant the order
    # a head start it never had.
    signal_time = signal_bar_close
    if decision_time is not None:
        signal_time = max(pd.Timestamp(decision_time), signal_bar_close)
    # 2. The effective floor: persisted deadline, policy deadline from the
    # persisted decision, and a caller's floor — the latest of them.
    if clock is not None:
        signal_time = max(signal_time, clock["decision_at"])
    earliest = execution.earliest_execution_time(signal_time, policy)
    if clock is not None:
        earliest = max(earliest, clock["earliest_execution_time"])
    if execution_floor is not None:
        floor = pd.Timestamp(execution_floor)
        if floor.tzinfo is None or floor < signal_time:
            raise decision_provenance.ClockViolation(
                f"execution floor {floor.isoformat()} precedes "
                f"the decision {signal_time.isoformat()}")
        earliest = max(earliest, floor)

    if execution_index is None:
        # Located from the deadline itself, so a persisted floor binds the
        # search exactly as it binds the check below.
        found = int(pd.Series(stamps).searchsorted(earliest, side="left"))
        found = max(found, signal_index + 1)
        if found >= len(stamps):
            found = None
        if found is None:
            raise NoExecutableBar(
                f"no stored bar opens at or after {earliest.isoformat()}; "
                f"signal {record.id} could not have been filled")
        entry_index = found
    else:
        entry_index = execution_index
    if entry_index <= signal_index:
        raise ValueError("execution must follow a completed signal bar")

    # Timestamp first, price second, and never the other way round. The
    # invariant is checked against the bar actually about to be used, and it
    # is checked *before* that bar's open is read: a caller handing over an
    # ineligible execution bar is refused without the price ever being
    # touched. Raising after the read would stop the bad number reaching the
    # result but not stop the look-ahead, and nothing downstream could tell
    # which of the two had happened.
    fill_stamp = feed.execution_timestamp(signal_index, entry_index)
    if fill_stamp < earliest:
        raise ValueError(
            f"fill at {fill_stamp.isoformat()} precedes the earliest "
            f"executable time {earliest.isoformat()}")
    _, raw_open = feed.execution_open(signal_index, entry_index)
    entry_fill = raw_open + slippage.index_points(raw_open) * direction

    # The levels are the strategy's own. They used to travel with the fill —
    # `stop = record.stop_loss + (entry_fill - record.entry)` — which kept
    # the arithmetic of R intact while moving both levels off the structure
    # that justified them, and recorded nothing to say it had happened. A
    # gap large enough to invalidate the trade now refuses it instead.
    plan = execution.plan_entry(
        record.action, planned_entry=record.entry,
        planned_stop=record.stop_loss, planned_target=record.target,
        actual_entry=entry_fill, policy=policy)
    stop, target = plan.stop, plan.target
    risk_per_unit = abs(entry_fill - stop)

    if not plan.accepted:
        return Outcome(
            signal_id=record.id,
            signal_time=(repository.as_utc(record.created_at).isoformat()
                         if record.created_at else ""),
            action=record.action,
            confidence=float(record.confidence or 0.0),
            trend=(record.context or {}).get("trend"),
            signal_bar_time=feed.timestamp(signal_index).isoformat(),
            entry_time=fill_stamp.isoformat(),
            timing_basis=timing_basis,
            signal_bar_close_time=signal_bar_close.isoformat(),
            earliest_execution_time=earliest.isoformat(),
            actual_fill_time=fill_stamp.isoformat(),
            entry=round(entry_fill, 2), stop=round(stop, 2),
            target=round(target, 2), risk_per_unit=round(risk_per_unit, 2),
            outcome=execution.REJECTED_GAP,
            entry_side=record.action, exit_side="",
            planned_entry=round(plan.planned_entry, 2),
            actual_entry=round(plan.actual_entry, 2),
            planned_stop=round(plan.planned_stop, 2),
            planned_target=round(plan.planned_target, 2),
            gap_amount=round(plan.gap_amount, 4),
            execution_policy=plan.execution_policy,
            exit_basis=plan.rejection_detail)

    # entry_index is determined above from the recorded execution clock.
    outcome_name, exit_index, level_exit = _resolve(
        feed, entry_index, record.action, entry_fill, stop, target, policy)

    out = Outcome(
        signal_id=record.id,
        signal_time=(repository.as_utc(record.created_at).isoformat()
                     if record.created_at else ""),
        action=record.action,
        confidence=float(record.confidence or 0.0),
        trend=(record.context or {}).get("trend"),
        signal_bar_time=feed.timestamp(signal_index).isoformat(),
        entry_time=fill_stamp.isoformat(),
        timing_basis=timing_basis,
        signal_bar_close_time=signal_bar_close.isoformat(),
        # The deadline, not the fill. These used to be the same value, which
        # made the invariant `fill >= earliest` unfalsifiable: it compared a
        # number with itself. `actual_fill_time` is the fill.
        earliest_execution_time=earliest.isoformat(),
        actual_fill_time=fill_stamp.isoformat(),
        entry=round(entry_fill, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        risk_per_unit=round(risk_per_unit, 2),
        outcome=outcome_name,
        entry_side=record.action,
        exit_side=execution.opposite(record.action),
        planned_entry=round(plan.planned_entry, 2),
        actual_entry=round(plan.actual_entry, 2),
        planned_stop=round(plan.planned_stop, 2),
        planned_target=round(plan.planned_target, 2),
        gap_amount=round(plan.gap_amount, 4),
        execution_policy=plan.execution_policy,
        exit_basis=(level_exit.reason if level_exit is not None else outcome_name),
        ambiguous_intrabar=bool(level_exit is not None
                                and level_exit.ambiguous_intrabar),
    )

    if exit_index is None:
        feed.seek(len(feed) - 1)
        out.mfe_points, out.mae_points = _excursions(
            feed, entry_index, len(feed) - 1, record.action, entry_fill)
    else:
        feed.seek(exit_index)
        bar = feed.bar(exit_index)
        # The price the bar actually offered. A stop the bar gapped through
        # fills at the open, not at the level — `resolve_levels` decides
        # that once, for the engines and for this study alike.
        exit_price = (level_exit.price if level_exit is not None
                      else float(bar["close"]))

        slip = slippage.index_points(exit_price) * direction
        fill = exit_price - slip
        # Legs from the side. `min`/`max` charged STT to whichever price was
        # higher, so on a losing long it taxed the entry as if it were the
        # sale and understated the bill on every loser in the study.
        money = execution.account(
            entry_side=record.action, entry_price=entry_fill, exit_price=fill,
            quantity=quantity, costs=costs,
            reference_entry=raw_open, reference_exit=exit_price)

        out.exit_time = feed.close_time(exit_index).isoformat()
        out.exit_price = round(fill, 2)
        out.bars_held = exit_index - entry_index + 1
        out.minutes_held = round(
            (feed.timestamp(exit_index) - feed.timestamp(entry_index))
            .total_seconds() / 60, 1)
        out.gross_points = round((fill - entry_fill) * direction, 2)
        out.r_multiple = (round(out.gross_points / risk_per_unit, 3)
                          if risk_per_unit else 0.0)
        out.charges = round(money.total_fees, 2)
        out.gross_pnl = round(money.gross_pnl, 2)
        out.execution_friction = round(money.execution_friction, 2)
        out.brokerage = round(money.brokerage, 2)
        out.statutory_fees = round(money.statutory_fees, 2)
        out.net_pnl = round(money.net_pnl, 2)
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
    # Entries the fill invalidated before the trade began. Counted here and
    # excluded from everything below, because a refused entry is not a loss.
    rejected_gap_entries: int = 0
    # Outcomes decided by the stop-first assumption rather than observed,
    # and exits that filled away from their level because the bar gapped
    # through it. Both are properties of the *sample*, so they belong beside
    # the win rate rather than in a footnote.
    ambiguous_trade_count: int = 0
    gapped_exits: int = 0
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
        elif row.outcome == execution.REJECTED_GAP:
            b.rejected_gap_entries += 1
        else:
            b.unresolved += 1
        if row.ambiguous_intrabar:
            b.ambiguous_trade_count += 1
        if row.exit_basis in (execution.STOP_GAP, execution.TARGET_GAP):
            b.gapped_exits += 1

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
    # What this replay assumed about execution and what it charged. A study
    # whose execution assumptions are not printed cannot be compared with a
    # backtest, which is the comparison it exists to support.
    execution: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


CAVEATS = [
    "Legacy signals lack source-bar timestamps: last-closed-bar alignment is inferred, "
    "not verified. "
    "Duplicate source bars are excluded. This is signal analytics, not executed-trade performance.",
    "Hypothetical. No order was placed; these are stored signals replayed "
    "against stored candles.",
    "Observations are not independent. The agent emits a signal every five "
    "minutes, so consecutive signals in one move describe the same move and "
    "a win rate over them overstates how much evidence there is.",
    "When one bar's range covers both the stop and the target, the stop is "
    "assumed to have filled first. Five-minute bars do not record which came "
    "first, and the pessimistic reading is the only safe one.",
    "Headline R multiples are GROSS, which is the signal-quality reading: "
    "it isolates whether the direction and the levels were right from what "
    "it would have cost to trade them. Net figures now charge the index "
    "backtest's flat per-round-trip figure rather than the option premium "
    "schedule, which this study used to apply to an index level and which "
    "billed about 3,400 rupees a round trip — roughly 2.3R at a "
    "twenty-point stop, a measurement of the mispricing and not of the "
    "signals. The flat figure is a configured assumption, not a verified "
    "contract note.",
    "A stop or target the bar gapped straight through fills at the bar's "
    "open, not at the level. Those exits are counted under `gapped_exits`, "
    "and the gap cuts both ways: a stop gapped through fills WORSE than the "
    "stop, while a target gapped through fills BETTER than the target. Both "
    "are the same rule — the price the bar actually opened at — and neither "
    "is a bias in the study's favour or against it.",
    "An entry whose fill had already reached its own stop or target is "
    "refused, not repriced. Those signals appear as "
    "`entry_rejected_due_to_gap` and are excluded from win rates: the "
    "trade did not happen, so counting it either way would be an invention.",
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
            quantity: int = EVALUATION_QUANTITY,
            policy: ExecutionPolicy | None = None) -> Selection:
    """Every genuine in-session signal, replayed against what happened next."""
    # Flat, because this study trades index points. See
    # EVALUATION_COST_PER_ROUND_TRIP for what the option schedule did here.
    costs = cost_model or FlatCostModel(
        per_round_trip=EVALUATION_COST_PER_ROUND_TRIP)
    slippage = slippage_model or SlippageModel()
    policy = policy or ExecutionPolicy()

    # Load only the columns this database has. A frozen archive from before
    # migration 0009 carries no persisted clocks; reading it must still work,
    # and those signals simply fall back to the JSON timing, labelled.
    only, missing = schema.loadable(db, SignalRecord)
    clocks_persisted = "decision_at" not in missing
    records = list(db.scalars(
        select(SignalRecord)
        .options(*only)
        .where(SignalRecord.symbol == symbol, SignalRecord.timeframe == timeframe)
        .order_by(SignalRecord.created_at)
    ).all())

    selection = SelectionReport(stored=len(records))
    # The research read: bars off the exchange grid — a 15:30 bar, a
    # duplicated or off-boundary stamp — are quarantined, not replayed
    # against. See `data.research` and `data.clock_grid`.
    candles = research.load_research_candles(db, symbol, timeframe)

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

        timing = (record.context or {}).get("timing") or {}
        # The persisted clocks first (TC-2). A column written at decision time
        # is the record; the JSON copy is what older rows have instead, and
        # is used for them — labelled — rather than for everything.
        #
        # A row that claims persisted clocks is held to them. If any is
        # missing or out of order the row fails closed — it is not quietly
        # re-read through the legacy JSON, which would replace the recorded
        # deadline with a weaker one computed from today's latency.
        persisted = (clocks_persisted
                     and record.clock_basis == decision_provenance.PERSISTED)
        floor = None
        if persisted:
            try:
                clock = decision_provenance.persisted(record, timeframe_minutes=5)
            except decision_provenance.ClockViolation:
                selection.invalid_timing += 1
                continue
            decision = clock["decision_at"]
            recorded_bar = clock["bar_open_time"]
            floor = clock["earliest_execution_time"]
        else:
            decision = pd.Timestamp(timing.get("signal_time") or stamped)
            recorded_bar = (pd.Timestamp(timing["bar_open_time"])
                            if timing.get("bar_open_time") else None)
        if decision.tzinfo is None:
            selection.invalid_timing += 1
            continue
        if recorded_bar is not None:
            bar_time = recorded_bar
            if bar_time.tzinfo is None or bar_time + pd.Timedelta(minutes=5) > decision:
                selection.invalid_timing += 1
                continue
            position = int(stamps.searchsorted(bar_time))
            if position >= len(feed) or stamps.iloc[position] != bar_time:
                selection.no_candle += 1
                continue
            basis = PERSISTED_CLOCK if persisted else "recorded_bar_time"
        else:
            position = int(stamps.searchsorted(decision-pd.Timedelta(minutes=5), side="right"))-1
            basis = "legacy_inferred_last_closed_bar"
            selection.legacy_inferred_timing += 1
        # Named `execution_bar`, not `execution`: the module of that name is
        # imported here, and a local binding would shadow it silently.
        #
        # `searchsorted` rather than `first_executable_index` because the
        # guard below must keep rejecting a signal whose executable bar is
        # not strictly after its own source bar, rather than quietly
        # advancing to the next one. The deadline is the shared one.
        #
        # A persisted deadline is the floor (TC-2): the policy latency may
        # push the fill later, never earlier than what the decision recorded.
        deadline = execution.earliest_execution_time(decision, policy)
        if floor is not None:
            deadline = max(deadline, floor)
        execution_bar = int(stamps.searchsorted(deadline, side="left"))
        if (position < 0 or execution_bar >= len(feed)
                or execution_bar <= position):
            selection.no_candle += 1
            continue
        # Never fill a signal at an earlier open or carry a late signal overnight.
        if (stamps.iloc[execution_bar].tz_convert("Asia/Kolkata").date()
                != decision.tz_convert("Asia/Kolkata").date()):
            selection.no_candle += 1
            continue
        if position in seen_bars:
            selection.duplicate_bars += 1
            continue
        seen_bars.add(position)
        # The recorded decision stamp travels with the signal so the
        # evaluator starts its own execution clock from the same instant
        # this loop used to choose the bar. Two clocks derived from
        # different moments would let one of them be satisfied while the
        # other was not.
        outcomes.append(evaluate_signal(feed, position, record, costs, slippage,
                        quantity, execution_index=execution_bar, timing_basis=basis,
                        policy=policy, decision_time=decision,
                        execution_floor=floor))

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
             include_outcomes: bool = True,
             policy: ExecutionPolicy | None = None) -> EvaluationReport:
    """Replay every genuine in-session signal against what happened next."""
    policy = policy or ExecutionPolicy()
    picked = collect(db, symbol, timeframe, cost_model, slippage_model, quantity,
                     policy)
    outcomes = picked.outcomes
    charged = cost_model or FlatCostModel(
        per_round_trip=EVALUATION_COST_PER_ROUND_TRIP)

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
        execution=policy.describe() | {
            "quantity": quantity,
            "cost_schedule_status": getattr(charged, "schedule_status", "assumed"),
            "turnover_basis": getattr(charged, "turnover_basis", "unstated"),
        },
    )
