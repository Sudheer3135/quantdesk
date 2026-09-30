"""Strategy v2's rules, as pure functions.

No clock, no database, no socket: every input arrives as an argument. That
is what lets the paper trader and a future backtest share one definition of
each rule instead of two that drift apart, and what lets every rule be
tested at the exact boundary it draws.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .. import market_calendar
from ..analytics import option_pricing
from ..analytics import plan as plan_builder
from .config import Rejection, V2Config

# ---- rejection codes ----------------------------------------------------
HOLD = "no_directional_signal"
NO_LEVELS = "signal_has_no_levels"
STALE_SIGNAL = "signal_too_old"
ENTRY_STATE = "entry_state_not_ready"
BIAS = "bias_disagrees_with_action"
OUTSIDE_WINDOW = "outside_entry_window"
EXPIRY_DAY = "expiry_day"
POSITION_OPEN = "position_already_open"
KILL_SWITCH = "kill_switch_engaged"
FEED_DOWN = "live_feed_not_healthy"
VIX_NO_HISTORY = "vix_history_insufficient"
VIX_NO_LIVE = "vix_live_unavailable"
VIX_HIGH = "vix_percentile_too_high"
VIX_SPIKE = "vix_spiking"
NO_EXPIRY = "no_expiry_far_enough"
CHAIN_NOT_READY = "chain_not_ready"
NO_IV = "implied_volatility_unsolvable"
NO_DELTA = "no_strike_in_delta_band"
STALE_QUOTE = "quote_too_old"
NO_DEPTH = "no_two_sided_quote"
WIDE_SPREAD = "spread_too_wide"
CHEAP = "premium_below_floor"
NO_DEFINED_RISK = "premium_risk_not_defined"
RISK_VETO = "risk_manager_vetoed"
PAST_STOP = "index_past_stop"
PAST_TARGET = "index_past_target"
NO_SPOT = "no_live_index_price"
ENTERED = "entered"

# ---- exit reasons -------------------------------------------------------
EXIT_PREMIUM_STOP = "premium_stop"
EXIT_INDEX_STOP = "index_stop"
EXIT_PREMIUM_TARGET = "premium_target"
EXIT_INDEX_TARGET = "index_target"
EXIT_SESSION_END = "session_end"
EXIT_TIME = "time_limit"
EXIT_MANUAL = "manual"
EXIT_RESTART = "closed_after_restart"


# --------------------------------------------------------------------------
# the signal
# --------------------------------------------------------------------------

def signal_rejection(signal: dict, *, now: datetime, cfg: V2Config) -> Rejection | None:
    """Does the desk's signal ask for a trade v2 is willing to take?"""
    action = signal.get("action")
    if action not in ("BUY", "SELL"):
        return Rejection(HOLD, f"the desk says {action or 'nothing'}")
    if signal.get("stop_loss") is None or signal.get("target") is None:
        return Rejection(NO_LEVELS, f"{action} carried no stop or target")

    # `generated_at` is stamped by whoever first saw the signal. The bar
    # `timestamp` is not used: the agent runs at an offset inside the bar,
    # so a perfectly fresh signal routinely carries a stamp minutes old.
    stamp = signal.get("generated_at")
    if stamp:
        try:
            made = datetime.fromisoformat(str(stamp))
            if made.tzinfo is not None:
                age = (now - made).total_seconds()
                if age > cfg.max_signal_age_seconds:
                    return Rejection(STALE_SIGNAL,
                                     f"signal is {age:.0f}s old, past the "
                                     f"{cfg.max_signal_age_seconds:.0f}s limit")
        except ValueError:
            pass

    plan = signal.get("plan") or {}
    entry_state = (plan.get("entry") or {}).get("state")
    bias = (plan.get("bias") or {}).get("label")
    if cfg.require_entry_states and entry_state not in cfg.require_entry_states:
        return Rejection(ENTRY_STATE,
                         f"entry state is {entry_state or 'unknown'}; v2 needs "
                         f"{', '.join(cfg.require_entry_states)}")
    if cfg.require_bias_agreement and not bias_agrees(bias, action):
        return Rejection(BIAS, f"{action} against a {bias or 'missing'} bias")
    return None


def level_rejection(action: str, spot: float, stop: float, target: float) -> Rejection | None:
    """Has the index already moved past the signal's stop or target?

    A trade whose stop is behind the current price is not a trade with small
    risk — its premise is gone. One already at its target has nothing left
    to make.
    """
    long_index = action == "BUY"
    if (spot <= stop) if long_index else (spot >= stop):
        return Rejection(PAST_STOP, f"index {spot:.2f} is already past the {stop:.2f} stop")
    if (spot >= target) if long_index else (spot <= target):
        return Rejection(PAST_TARGET, f"index {spot:.2f} is already past the {target:.2f} target")
    return None


def bias_agrees(bias: str | None, action: str) -> bool:
    return ((action == "BUY" and bias == plan_builder.BULLISH)
            or (action == "SELL" and bias == plan_builder.BEARISH))


def option_type_for(action: str) -> str:
    return "CE" if action == "BUY" else "PE"


# --------------------------------------------------------------------------
# the calendar
# --------------------------------------------------------------------------

def in_entry_window(moment_ist: datetime, cfg: V2Config) -> bool:
    return cfg.entry_start <= moment_ist.time() <= cfg.entry_end


def _is_session(day: date) -> bool:
    state = market_calendar.is_session(day)
    # A year with no holiday list counts its weekdays. The gate this feeds
    # only ever asks for *more* sessions, so the error is towards caution.
    return state if state is not None else day.weekday() < 5


def sessions_until(today: date, expiry: date) -> int:
    """Trading sessions after `today`, up to and including `expiry`."""
    count, day = 0, today + timedelta(days=1)
    while day <= expiry:
        if _is_session(day):
            count += 1
        day += timedelta(days=1)
    return count


def choose_expiry(listed: Iterable[date], today: date, cfg: V2Config) -> date | None:
    """The nearest listed weekly with enough sessions left to hold it."""
    for expiry in sorted(set(listed)):
        if expiry >= today and sessions_until(today, expiry) >= cfg.min_sessions_to_expiry:
            return expiry
    return None


def is_expiry_day(today: date, listed: Iterable[date]) -> bool:
    return today in set(listed)


# --------------------------------------------------------------------------
# India VIX
# --------------------------------------------------------------------------

@dataclass
class VixReading:
    ok: bool
    code: str
    detail: str
    live: float | None = None
    previous_close: float | None = None
    percentile: float | None = None
    spike_pct: float | None = None
    history_sessions: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def percentile_rank(history: Sequence[float], value: float) -> float:
    """Share of past sessions that closed at or below `value`, in percent."""
    if not history:
        raise ValueError("no history to rank against")
    return 100.0 * sum(1 for v in history if v <= value) / len(history)


def vix_gate(history: Sequence[float], live: float | None, cfg: V2Config) -> VixReading:
    """Is volatility calm enough to buy premium?

    `history` is daily closes, oldest first, ending *before* today — ranking
    today against a list that already contains today would let the reading
    vote on itself.

    Fails closed. Without enough history the gate cannot tell a calm market
    from an expensive one, and buying premium blind to that is exactly the
    trade this gate exists to stop.
    """
    sessions = len(history)
    if sessions < cfg.vix_min_history:
        return VixReading(False, VIX_NO_HISTORY,
                          f"{sessions} sessions of VIX history; the gate needs "
                          f"{cfg.vix_min_history}", live=live,
                          history_sessions=sessions)
    if live is None or live <= 0:
        return VixReading(False, VIX_NO_LIVE, "no live India VIX reading",
                          history_sessions=sessions)

    window = list(history)[-cfg.vix_lookback_sessions:]
    pct = percentile_rank(window, live)
    previous = float(history[-1])
    spike = (live / previous - 1.0) * 100.0 if previous > 0 else 0.0
    reading = VixReading(True, "ok", "", live=live, previous_close=previous,
                         percentile=round(pct, 1), spike_pct=round(spike, 2),
                         history_sessions=sessions)

    if pct >= cfg.vix_block_percentile:
        reading.ok, reading.code = False, VIX_HIGH
        reading.detail = (f"VIX {live:.2f} is above {pct:.0f}% of the last "
                          f"{len(window)} sessions; the ceiling is "
                          f"{cfg.vix_block_percentile:.0f}%")
    elif spike >= cfg.vix_spike_pct:
        reading.ok, reading.code = False, VIX_SPIKE
        reading.detail = (f"VIX is up {spike:.1f}% on yesterday's "
                          f"{previous:.2f} close; premium is being repriced")
    else:
        reading.detail = (f"VIX {live:.2f}, {pct:.0f}th percentile, "
                          f"{spike:+.1f}% on the day")
    return reading


# --------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    """One live quote the chain can offer."""
    strike: float
    option_type: str
    ltp: float
    bid: float | None
    ask: float | None
    age_seconds: float
    token: str | None = None
    symbol: str | None = None
    lot_size: int | None = None


@dataclass
class Pick:
    candidate: Candidate
    mid: float
    iv: float
    delta: float
    spread_pct: float | None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        c = self.candidate
        return {
            "strike": c.strike, "option_type": c.option_type,
            "symbol": c.symbol, "token": c.token, "lot_size": c.lot_size,
            "ltp": c.ltp, "bid": c.bid, "ask": c.ask,
            "quote_age_seconds": round(c.age_seconds, 2),
            "mid": round(self.mid, 2), "iv": round(self.iv, 4),
            "delta": round(self.delta, 3),
            "spread_pct": round(self.spread_pct, 2) if self.spread_pct is not None else None,
            "reasons": self.reasons,
        }


def _mid(c: Candidate) -> float:
    if c.bid and c.ask and c.ask >= c.bid > 0:
        return (c.bid + c.ask) / 2
    return c.ltp


# The checks `_liquidity_problem` applies, in the order it applies them, and
# the code each one refuses with. It stops at the first failure, so the checks
# after a failing one were never evaluated for that contract. Kept beside the
# function so the evidence trace cannot drift from it.
LIQUIDITY_CHECKS = (("quote_age", STALE_QUOTE), ("two_sided_quote", NO_DEPTH),
                    ("spread", WIDE_SPREAD), ("premium_floor", CHEAP))


def _liquidity_problem(c: Candidate, mid: float, cfg: V2Config) -> Rejection | None:
    if c.age_seconds > cfg.max_quote_age_seconds:
        return Rejection(STALE_QUOTE, f"{c.strike:.0f}{c.option_type} last quoted "
                                      f"{c.age_seconds:.1f}s ago")
    if not c.bid or not c.ask or c.ask < c.bid:
        return Rejection(NO_DEPTH, f"{c.strike:.0f}{c.option_type} has no two-sided quote")
    spread = (c.ask - c.bid) / mid * 100 if mid > 0 else float("inf")
    if spread > cfg.max_spread_pct:
        return Rejection(WIDE_SPREAD, f"{c.strike:.0f}{c.option_type} spread "
                                      f"{spread:.1f}% of premium")
    if c.ask < cfg.min_premium:
        return Rejection(CHEAP, f"{c.strike:.0f}{c.option_type} asks {c.ask:.2f}, "
                                f"under the {cfg.min_premium:.0f} floor")
    return None


def pick_contract(candidates: Iterable[Candidate], *, option_type: str, spot: float,
                  years: float, cfg: V2Config,
                  rate: float = option_pricing.DEFAULT_RATE,
                  trace: dict | None = None,
                  ) -> tuple[Pick | None, Rejection | None]:
    """The liquid strike whose delta is nearest the target, inside the band.

    Delta is implied from each contract's own quote rather than from one
    assumed volatility: the smile makes a single IV place the 0.50 delta a
    strike or more away from where the market actually has it.

    `trace`, when given, is filled with one entry per candidate saying how
    far the selection took it and why it stopped there (`alternatives`), and
    what the selection concluded (`result`). It is written to and never
    read: the pick is the same with or without it (Phase 3B evidence).
    """
    candidates = list(candidates)
    steps = _Trace(candidates, option_type) if trace is not None else None
    pool = [c for c in candidates if c.option_type == option_type]
    if not pool or years <= 0:
        if steps:
            steps.finish(trace, refused=CHAIN_NOT_READY)
        return None, Rejection(CHAIN_NOT_READY,
                               f"no {option_type} quotes for the chosen expiry")

    priced: list[Pick] = []
    for c in pool:
        mid = _mid(c)
        iv = option_pricing.implied_volatility(mid, spot, c.strike, years, rate,
                                               kind=option_type)
        if steps:
            steps.priced(c, mid, iv)
        if iv is None:
            continue
        delta = abs(option_pricing.greeks(spot, c.strike, years, iv, rate,
                                          kind=option_type).delta)
        spread = ((c.ask - c.bid) / mid * 100
                  if c.bid and c.ask and c.ask >= c.bid and mid > 0 else None)
        priced.append(Pick(c, mid, iv, delta, spread))
        if steps:
            steps.delta(c, delta, spread, cfg.min_delta <= delta <= cfg.max_delta)
    if not priced:
        if steps:
            steps.finish(trace, refused=NO_IV)
        return None, Rejection(NO_IV, "no quote reproduced a volatility")

    band = sorted((p for p in priced if cfg.min_delta <= p.delta <= cfg.max_delta),
                  key=lambda p: (abs(p.delta - cfg.target_delta), p.mid))
    if not band:
        nearest = min(priced, key=lambda p: abs(p.delta - cfg.target_delta))
        if steps:
            steps.finish(trace, refused=NO_DELTA)
        return None, Rejection(
            NO_DELTA,
            f"nearest delta {nearest.delta:.2f} at {nearest.candidate.strike:.0f}, "
            f"outside {cfg.min_delta:.2f}–{cfg.max_delta:.2f}")

    if steps:
        steps.ranked(band)
    first_problem: Rejection | None = None
    for p in band:
        problem = _liquidity_problem(p.candidate, p.mid, cfg)
        if steps:
            steps.liquidity(p.candidate, problem)
        if problem is None:
            p.reasons.append(f"delta {p.delta:.2f} at IV {p.iv:.1%}, the nearest "
                             f"liquid strike to {cfg.target_delta:.2f}")
            if steps:
                steps.finish(trace, selected=p.candidate)
            return p, None
        first_problem = first_problem or problem
    if steps:
        steps.finish(trace, refused=first_problem.code)
    return None, first_problem


class _Trace:
    """What `pick_contract` did with each candidate, in the order it did it.

    Every candidate starts `not_assessed` at the stage before the first one,
    and moves forward only when the selector actually evaluated it. A stage
    the selector never reached for a contract stays unreached — a contract
    ranked behind the one selected was not refused, it was not looked at.
    """

    def __init__(self, candidates: list[Candidate], option_type: str) -> None:
        self._rows: dict[int, dict] = {}
        self._order: list[int] = []
        for c in candidates:
            row = {"token": c.token, "symbol": c.symbol, "strike": c.strike,
                   "option_type": c.option_type, "stage": "option_type",
                   "status": "not_assessed", "code": None}
            if c.option_type != option_type:
                row["status"] = "excluded"
                row["code"] = "other_option_type"
            self._rows[id(c)] = row
            self._order.append(id(c))

    def priced(self, c: Candidate, mid: float, iv: float | None) -> None:
        row = self._rows[id(c)]
        two_sided = bool(c.bid and c.ask and c.ask >= c.bid > 0)
        row.update(stage="implied_volatility", mid=mid,
                   mid_basis="bid_ask_mid" if two_sided else "ltp_fallback",
                   iv=iv, status="excluded" if iv is None else "passed",
                   code=NO_IV if iv is None else None)

    def delta(self, c: Candidate, delta: float, spread: float | None, inside: bool) -> None:
        self._rows[id(c)].update(stage="delta_band", delta=delta, spread_pct=spread,
                                 status="passed" if inside else "excluded",
                                 code=None if inside else NO_DELTA)

    def ranked(self, band: list[Pick]) -> None:
        for rank, p in enumerate(band, start=1):
            self._rows[id(p.candidate)].update(band_rank=rank, stage="liquidity",
                                               status="not_assessed")

    def liquidity(self, c: Candidate, problem: Rejection | None) -> None:
        failed_at = next((i for i, (_, code) in enumerate(LIQUIDITY_CHECKS)
                          if problem is not None and code == problem.code), None)
        checks = []
        for i, (name, _) in enumerate(LIQUIDITY_CHECKS):
            if failed_at is None or i < failed_at:
                checks.append({"check": name, "status": "passed"})
            elif i == failed_at:
                checks.append({"check": name, "status": "failed"})
            else:
                checks.append({"check": name, "status": "not_assessed"})
        self._rows[id(c)].update(
            liquidity_checks=checks,
            status="selected" if problem is None else "failed",
            code=None if problem is None else problem.code,
            reason=None if problem is None else problem.detail)

    def finish(self, trace: dict, *, selected: Candidate | None = None,
               refused: str | None = None) -> None:
        trace["alternatives"] = [dict(self._rows[key]) for key in self._order]
        trace["result"] = {"status": "selected" if selected is not None else "refused",
                           "token": selected.token if selected is not None else None,
                           "code": refused}


# --------------------------------------------------------------------------
# the levels
# --------------------------------------------------------------------------

@dataclass
class Levels:
    stop: float
    target: float
    stop_basis: str
    target_basis: str
    at_index_stop: float
    at_index_target: float

    def to_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}


def premium_levels(*, entry: float, strike: float, option_type: str, years: float,
                   iv: float, index_stop: float, index_target: float, cfg: V2Config,
                   rate: float = option_pricing.DEFAULT_RATE) -> Levels:
    """Stop and target in premium: the nearer of the index level and the cap.

    The premium at the index levels is projected with Black-Scholes at the
    contract's own implied volatility — no quote exists for a level the
    index has not reached. The percentage caps bound what the projection
    cannot see: a volatility collapse can take 30% off a premium while the
    index stands still.
    """
    at_stop = option_pricing.price(index_stop, strike, years, iv, rate, kind=option_type)
    at_target = option_pricing.price(index_target, strike, years, iv, rate, kind=option_type)
    floor = entry * (1 - cfg.premium_stop_pct / 100)
    cap = entry * (1 + cfg.premium_target_pct / 100)
    return Levels(
        stop=max(at_stop, floor), target=min(at_target, cap),
        stop_basis="index_stop" if at_stop >= floor else "premium_pct",
        target_basis="index_target" if at_target <= cap else "premium_pct",
        at_index_stop=at_stop, at_index_target=at_target,
    )


# --------------------------------------------------------------------------
# the exit
# --------------------------------------------------------------------------

def exit_reason(*, direction: str, bid: float | None, spot: float | None,
                premium_stop: float, premium_target: float,
                index_stop: float, index_target: float,
                now_ist: datetime, opened_at_ist: datetime,
                cfg: V2Config) -> str | None:
    """Which rule closes the position now, if any.

    Losses are checked before gains. When a fast move carries a quote past
    both in one update there is no knowing which came first, and assuming
    the good one is how a paper record flatters itself.
    """
    long_index = direction == "BUY"
    if bid is not None and bid <= premium_stop:
        return EXIT_PREMIUM_STOP
    if spot is not None and (spot <= index_stop if long_index else spot >= index_stop):
        return EXIT_INDEX_STOP
    if bid is not None and bid >= premium_target:
        return EXIT_PREMIUM_TARGET
    if spot is not None and (spot >= index_target if long_index else spot <= index_target):
        return EXIT_INDEX_TARGET
    if now_ist.time() >= cfg.session_exit:
        return EXIT_SESSION_END
    if (now_ist - opened_at_ist).total_seconds() >= cfg.max_minutes_in_trade * 60:
        return EXIT_TIME
    return None
