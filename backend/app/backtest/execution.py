"""One execution truth: fills, levels, latency, legs and net P&L.

Four engines here simulate the same act of trading — the index backtest,
the modelled option backtest, the archive-priced option-buying strategy and
the signal evaluator. Each had grown its own answer to the same four
questions, and the answers disagreed:

  **Where do the stop and target sit when the fill is not the planned
  entry?** The index engine and the evaluator translated both levels by the
  gap, silently converting a trade whose premise had already broken into a
  trade with the original risk *measured from somewhere else*. Nothing
  recorded that it had happened. The option engines instead refused the
  entry outright. Two engines, two policies, one strategy.

  **What price does a stop fill at when the bar gaps through it?** The
  engines took `min(stop, open)` — a long stopped at 99 on a bar that opens
  at 97 exits at 97, which is the only price that existed. The evaluator
  took the stop itself, reporting a fill at a price the market never
  offered, and every number downstream of it was better than reality.

  **Which filled first when one bar covers both levels?** Everyone assumed
  the stop, correctly, and nobody counted how often the assumption was load
  bearing. A result that rests on an assumption 40% of the time and one
  that rests on it twice are not the same result.

  **Which leg was the buy?** Two of them inferred it with
  `min(entry, exit)` / `max(entry, exit)`, which is direction inference by
  price order. On a losing long it names the *exit* the buy, charges STT to
  the wrong leg and stamp duty to the other, and understates the bill.

So the answers live here once, and the callers ask. This is deliberately a
library of small functions over a framework: the engines keep their own
loops, their own sizing and their own reasons for exiting, because the
point is a shared *definition* of execution, not a shared control flow.

Nothing here decides whether a trade is good. Every default is the
pessimistic one, chosen before seeing what it does to the result — which is
the only moment at which such a choice can honestly be made.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pandas as pd

# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

# What happens to the stop and target when the fill is not the planned entry.
#
# KEEP_PLANNED is the research default. The strategy's levels are statements
# about *the market* — a stop below a swing low, a target at a prior high —
# not offsets from wherever the order happened to fill. Translating them by
# the gap keeps the arithmetic of R intact while quietly moving the levels
# off the structure that justified them, and produces a trade nobody chose.
KEEP_PLANNED = "keep_planned_levels"

# The alternative, for a strategy that genuinely defines its levels relative
# to the fill (a fixed-points stop, say). It is a real policy and it is
# available, but it has to be asked for by name and it is labelled on every
# trade it touches, so no result silently contains a mixture of the two.
SHIFT_WITH_FILL = "shift_levels_with_fill"

GAPPED_ENTRY_POLICIES = (KEEP_PLANNED, SHIFT_WITH_FILL)

# Which level is assumed to have filled first when one bar touches both and
# the bar's own open does not settle it.
STOP_FIRST = "stop_first"
TARGET_FIRST = "target_first"
INTRABAR_POLICIES = (STOP_FIRST, TARGET_FIRST)

# Why an entry was refused. One string, so the count means the same thing in
# every engine and in the ledger.
REJECTED_GAP = "entry_rejected_due_to_gap"

# Exit reasons. `stop_gap` is distinct from `stop` on purpose: it marks the
# trades whose exit price came from the bar rather than from the level, and
# those are the trades a reviewer should look at first.
STOP = "stop"
STOP_GAP = "stop_gap"
TARGET = "target"
TARGET_GAP = "target_gap"


@dataclass(frozen=True)
class ExecutionPolicy:
    """Every execution assumption a run makes, in one object it can print.

    A result whose execution policy is not stated cannot be compared with
    another result, because nothing records whether the difference was the
    strategy or the assumptions.
    """

    gapped_entry: str = KEEP_PLANNED
    intrabar: str = STOP_FIRST

    # Seconds between the decision and the earliest moment an order could be
    # working in the market. Zero is not a claim that execution is
    # instantaneous — it is the historical convention this platform has
    # always used (fill at the next bar's open), stated explicitly so that
    # it can be varied. Any positive value pushes the fill to the first bar
    # that opens at or after `signal_time + latency`.
    latency_seconds: float = 0.0

    # The reward-to-risk a fill must still offer for the entry to stand.
    # Zero means only the degenerate cases are refused: a fill already at or
    # through the stop (no risk to measure, and a position size that divides
    # by it explodes), or already at or through the target.
    min_reward_risk: float = 0.0

    def __post_init__(self) -> None:
        if self.gapped_entry not in GAPPED_ENTRY_POLICIES:
            raise ValueError(f"unknown gapped-entry policy: {self.gapped_entry}")
        if self.intrabar not in INTRABAR_POLICIES:
            raise ValueError(f"unknown intrabar policy: {self.intrabar}")
        if not math.isfinite(self.latency_seconds) or self.latency_seconds < 0:
            raise ValueError("latency cannot be negative or non-finite")
        if not math.isfinite(self.min_reward_risk) or self.min_reward_risk < 0:
            raise ValueError("minimum reward:risk cannot be negative")

    def describe(self) -> dict:
        return {
            "gapped_entry_policy": self.gapped_entry,
            "intrabar_ambiguity_policy": self.intrabar,
            "execution_latency_seconds": self.latency_seconds,
            "min_reward_risk": self.min_reward_risk,
            "notes": (
                "Levels are the strategy's own unless the gapped-entry "
                "policy says otherwise. When one bar covers stop and "
                "target, the bar's open decides if it can and the policy "
                "decides if it cannot; ambiguous bars are counted, not "
                "hidden. A stop that gapped fills at the bar's open, never "
                "at the level."),
        }


DEFAULT_POLICY = ExecutionPolicy()


def _direction(side: str) -> int:
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, not {side!r}")
    return 1 if side == "BUY" else -1


def opposite(side: str) -> str:
    """The side that closes a position opened on `side`."""
    return "SELL" if _direction(side) == 1 else "BUY"


# ---------------------------------------------------------------------------
# latency (FL-4)
# ---------------------------------------------------------------------------

def earliest_execution_time(signal_time, policy: ExecutionPolicy = DEFAULT_POLICY):
    """The first instant an order from this decision could be working.

    Returned rather than assumed so that the invariant
    `earliest_execution_time >= signal_time + latency` is something a test
    can read off the trade instead of trusting the loop that built it.
    """
    return pd.Timestamp(signal_time) + pd.Timedelta(seconds=policy.latency_seconds)


def first_executable_index(stamps, after_index: int, signal_time,
                           policy: ExecutionPolicy = DEFAULT_POLICY) -> int | None:
    """Index of the first bar that may legally be traded on, or None.

    "May legally be traded on" means two things at once and both are
    enforced here rather than in four loops: the bar comes strictly after
    the bar the decision was made on, and it opens at or after the latency
    deadline. A bar that opens *before* the order could exist is not an
    execution opportunity however convenient its price.
    """
    earliest = earliest_execution_time(signal_time, policy)
    start = after_index + 1
    if start >= len(stamps):
        return None
    # searchsorted with side="left" lands on the first stamp >= earliest,
    # which is exactly the boundary case: a bar opening at the deadline is
    # eligible, one opening a microsecond earlier is not.
    found = int(pd.Series(stamps).searchsorted(earliest, side="left"))
    index = max(start, found)
    return index if index < len(stamps) else None


# ---------------------------------------------------------------------------
# entry (SE-2)
# ---------------------------------------------------------------------------

@dataclass
class EntryDecision:
    """What the fill did to the trade that was planned.

    Every field a reviewer needs to reconstruct the decision is stored, not
    derived later: `planned_entry` and `actual_entry` together with the
    policy explain the levels, and `gap_amount` explains why they were in
    question at all.
    """

    accepted: bool
    side: str
    planned_entry: float
    actual_entry: float
    planned_stop: float
    planned_target: float
    stop: float
    target: float
    gap_amount: float
    execution_policy: str
    risk_per_unit: float
    reward_per_unit: float
    reward_risk: float | None = None
    rejection: str | None = None
    rejection_detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def plan_entry(side: str, *, planned_entry: float, planned_stop: float,
               planned_target: float, actual_entry: float,
               policy: ExecutionPolicy = DEFAULT_POLICY) -> EntryDecision:
    """Decide the trade's levels given the price it actually filled at.

    The gap between the planned entry and the fill is real information: it
    says the market moved between the decision and the order. Under the
    default policy that information is allowed to invalidate the trade —
    which it does when the fill has already reached the stop or the target,
    or when what is left is not worth the risk — and it is never allowed to
    silently redefine it.
    """
    direction = _direction(side)
    gap = float(actual_entry) - float(planned_entry)

    if policy.gapped_entry == SHIFT_WITH_FILL:
        stop = float(planned_stop) + gap
        target = float(planned_target) + gap
    else:
        stop = float(planned_stop)
        target = float(planned_target)

    risk = (float(actual_entry) - stop) * direction
    reward = (target - float(actual_entry)) * direction
    ratio = (reward / risk) if risk > 0 else None

    decision = EntryDecision(
        accepted=True, side=side,
        planned_entry=float(planned_entry), actual_entry=float(actual_entry),
        planned_stop=float(planned_stop), planned_target=float(planned_target),
        stop=stop, target=target, gap_amount=gap,
        execution_policy=policy.gapped_entry,
        risk_per_unit=risk, reward_per_unit=reward, reward_risk=ratio)

    # A fill at or through the stop is not a trade with tiny risk. It is a
    # trade whose premise is already false, and sizing off the remaining
    # sliver is how one gap became 654 lots and a fictional 473% return.
    if risk <= 0:
        decision.accepted = False
        decision.rejection = REJECTED_GAP
        decision.rejection_detail = (
            f"fill {actual_entry:.2f} is at or through the {stop:.2f} stop "
            f"(planned entry {planned_entry:.2f}, gap {gap:+.2f})")
    elif reward <= 0:
        decision.accepted = False
        decision.rejection = REJECTED_GAP
        decision.rejection_detail = (
            f"fill {actual_entry:.2f} is at or past the {target:.2f} target "
            f"(planned entry {planned_entry:.2f}, gap {gap:+.2f})")
    elif policy.min_reward_risk > 0 and ratio is not None and ratio < policy.min_reward_risk:
        decision.accepted = False
        decision.rejection = REJECTED_GAP
        decision.rejection_detail = (
            f"fill {actual_entry:.2f} leaves {ratio:.2f} reward:risk, under "
            f"the {policy.min_reward_risk:.2f} minimum "
            f"(planned entry {planned_entry:.2f}, gap {gap:+.2f})")
    return decision


# ---------------------------------------------------------------------------
# exit (PL-3, PL-4)
# ---------------------------------------------------------------------------

@dataclass
class LevelExit:
    """A stop or target exit, priced at something the market offered."""

    price: float
    reason: str
    level: float
    gapped: bool = False
    ambiguous_intrabar: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def touched(side: str, *, high: float, low: float, stop: float,
            target: float) -> tuple[bool, bool]:
    """Did this bar's range reach the stop, the target, or both?"""
    if _direction(side) == 1:
        return (float(low) <= stop, float(high) >= target)
    return (float(high) >= stop, float(low) <= target)


def stop_fill_price(side: str, stop: float, bar_open: float) -> float:
    """What a stop actually fills at on a bar that reached it.

    When the bar opens beyond the stop the level was never available: the
    first price of the session or the bar *is* the fill, and it is worse
    than the stop. A long stopped at 99 on a bar that opens at 97 exits at
    97. Reporting 99 there is not conservatism, it is a price that did not
    exist, and it flatters every figure computed from it.
    """
    if _direction(side) == 1:
        return min(float(stop), float(bar_open))
    return max(float(stop), float(bar_open))


def target_fill_price(side: str, target: float, bar_open: float) -> float:
    """What a target fills at on a bar that reached it.

    Symmetric with the stop in one respect only: a bar that *opens* past the
    target fills at the open, because that is the price that existed. The
    asymmetry is deliberate elsewhere — a target reached mid-bar fills at
    the target, never better, since nothing in a five-minute bar says the
    order would have been filled any deeper into the move.
    """
    if _direction(side) == 1:
        return max(float(target), float(bar_open)) if float(bar_open) >= float(target) else float(target)
    return min(float(target), float(bar_open)) if float(bar_open) <= float(target) else float(target)


def resolve_levels(side: str, *, bar_open: float, high: float, low: float,
                   stop: float, target: float,
                   policy: ExecutionPolicy = DEFAULT_POLICY) -> LevelExit | None:
    """Which level closed the trade on this bar, at what price, and how sure.

    Three cases, and only the third is a guess:

      Only one level was touched. Priced from the bar if it gapped through,
      from the level otherwise.

      Both were touched and the bar's *open* is already beyond one of them.
      That settles it — the open is the bar's first price, so the level it
      is past was reached before the other could be. This is the
      "higher-resolution data proves otherwise" case, using the only
      higher-resolution datum a candle carries.

      Both were touched and the open sits between them. Nothing in the bar
      says which came first. The policy decides, the default is the stop,
      and the trade is marked `ambiguous_intrabar` so the count of trades
      resting on that assumption is visible in the result instead of being
      an unstated property of the sample.
    """
    hit_stop, hit_target = touched(side, high=high, low=low, stop=stop, target=target)
    if not hit_stop and not hit_target:
        return None

    direction = _direction(side)
    open_through_stop = (float(bar_open) - float(stop)) * direction <= 0
    open_through_target = (float(bar_open) - float(target)) * direction >= 0

    if hit_stop and hit_target:
        if open_through_stop:
            price = stop_fill_price(side, stop, bar_open)
            return LevelExit(price=price, reason=STOP_GAP if price != stop else STOP,
                             level=float(stop), gapped=price != stop)
        if open_through_target:
            price = target_fill_price(side, target, bar_open)
            return LevelExit(price=price, reason=TARGET_GAP if price != target else TARGET,
                             level=float(target), gapped=price != target)
        if policy.intrabar == TARGET_FIRST:
            return LevelExit(price=float(target), reason=TARGET, level=float(target),
                             ambiguous_intrabar=True)
        return LevelExit(price=float(stop), reason=STOP, level=float(stop),
                         ambiguous_intrabar=True)

    if hit_stop:
        price = stop_fill_price(side, stop, bar_open)
        return LevelExit(price=price, reason=STOP_GAP if price != stop else STOP,
                         level=float(stop), gapped=price != stop)

    price = target_fill_price(side, target, bar_open)
    return LevelExit(price=price, reason=TARGET_GAP if price != target else TARGET,
                     level=float(target), gapped=price != target)


# ---------------------------------------------------------------------------
# money (CA-1, CA-2)
# ---------------------------------------------------------------------------

@dataclass
class Accounting:
    """One closed trade's money, with the legs it was actually charged on.

    `gross_pnl - execution_friction - total_fees == net_pnl` holds by
    construction and is checked below, because the three of them being
    reported separately is only useful if they still add up.
    """

    entry_side: str
    entry_price: float
    exit_side: str
    exit_price: float
    quantity: int

    buy_price: float
    sell_price: float

    gross_pnl: float
    execution_friction: float
    brokerage: float
    statutory_fees: float
    total_fees: float
    net_pnl: float
    breakdown: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def legs(entry_side: str, entry_price: float, exit_price: float) -> tuple[float, float]:
    """Which price was bought at and which was sold at.

    From the *side*, never from which number is larger. `min`/`max` reads
    direction off price order, so on a losing long — bought at 120, sold at
    100 — it decides the sale happened at 120. STT is charged on the sell
    leg and stamp duty on the buy, so the bill lands on the wrong legs and
    comes out too small, making losing trades look cheaper than winners.
    """
    if _direction(entry_side) == 1:
        return float(entry_price), float(exit_price)
    return float(exit_price), float(entry_price)


def account(*, entry_side: str, entry_price: float, exit_price: float,
            quantity: int, costs, reference_entry: float | None = None,
            reference_exit: float | None = None,
            exit_side: str | None = None) -> Accounting:
    """Every rupee of one closed round trip, separated by what caused it.

    `entry_price` and `exit_price` are what was *filled*. The optional
    references are the prices before slippage — the mid, or the level the
    rule fired at. Given both, the run can say how much of the result was
    the market moving and how much was the cost of trading it:

      gross      what the reference-to-reference move was worth
      friction   what crossing the spread and the slippage took
      fees       brokerage and statutory charges on the filled legs
      net        what the account actually changed by

    Fees are charged on the filled premium × quantity of each leg, which for
    an option is the only correct base. Charging option rates on an index
    level — 24,000 a point instead of a hundred-odd rupees of premium —
    produces a bill roughly two hundred times too large and turns every
    net figure into a measurement of the mistake.
    """
    exit_side = exit_side or opposite(entry_side)
    if exit_side == entry_side:
        raise ValueError("a round trip cannot open and close on the same side")

    direction = _direction(entry_side)
    ref_in = float(reference_entry if reference_entry is not None else entry_price)
    ref_out = float(reference_exit if reference_exit is not None else exit_price)

    buy_price, sell_price = legs(entry_side, entry_price, exit_price)
    charges = costs.round_trip(buy_price=buy_price, sell_price=sell_price,
                               quantity=quantity)

    gross = (ref_out - ref_in) * direction * quantity
    friction = (abs(float(entry_price) - ref_in)
                + abs(float(exit_price) - ref_out)) * quantity
    brokerage = float(getattr(charges, "brokerage", 0.0))
    total_fees = float(charges.total)
    net = gross - friction - total_fees

    # The same number by a different route: what the fills alone say. If
    # these disagree the friction has the wrong sign somewhere — a
    # *favourable* slippage would make the subtraction above a lie while
    # every individual figure still looked plausible.
    traded = (float(exit_price) - float(entry_price)) * direction * quantity - total_fees
    if abs(traded - net) > 1e-6 * max(1.0, abs(traded)):
        raise ValueError(
            f"execution accounting does not reconcile: fills give {traded:.6f}, "
            f"gross-friction-fees gives {net:.6f}")

    return Accounting(
        entry_side=entry_side, entry_price=float(entry_price),
        exit_side=exit_side, exit_price=float(exit_price), quantity=int(quantity),
        buy_price=buy_price, sell_price=sell_price,
        gross_pnl=gross, execution_friction=friction,
        brokerage=brokerage, statutory_fees=total_fees - brokerage,
        total_fees=total_fees, net_pnl=net, breakdown=charges.to_dict())
