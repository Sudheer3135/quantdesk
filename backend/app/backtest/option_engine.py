"""Option-buying backtest.

The index backtest answers "was the direction right?". This answers the
question that decides your account: "would buying the option have made
money?"

They are not the same question, and the gap between them is where most
retail option strategies quietly die. A trade can be directionally correct,
hit its index target, and still close at a loss because time decay took more
than delta gave.

How this works:

  1. The signal engine produces a BUY or SELL on the index, exactly as
     before. Nothing about signal generation changes.
  2. That direction is converted into a contract — a call for BUY, a put
     for SELL — at a chosen strike.
  3. Entry premium is priced with Black-Scholes at the next bar's open.
  4. Every subsequent bar reprices the option at that bar's index level and
     the remaining time to expiry. Decay is therefore charged continuously,
     not estimated at the end.
  5. Exits still trigger on index levels, because that is what the strategy
     watches, but profit and loss is measured in premium.

The result is usually worse than the index backtest. That is the point.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..analytics import option_pricing, signal_engine
from ..risk.manager import DayState, RiskConfig, evaluate
from . import costs as costs_module
from . import execution
from .costs import CostModel, FlatCostModel, SlippageModel, buy_fill, describe, sell_fill
from .execution import ExecutionPolicy
from .feed import HistoricalFeed
from .measurement import PositionLedger, provenance


@dataclass
class OptionTrade:
    entry_time: str
    exit_time: str
    direction: str            # BUY | SELL on the index
    option: str               # e.g. "24600 CE"
    strike: float
    kind: str
    index_entry: float
    index_exit: float
    premium_entry: float
    premium_exit: float
    lots: int
    quantity: int
    pnl: float
    r_multiple: float
    decay_cost: float         # premium lost purely to time
    exit_reason: str
    confidence: float
    # Charges broken out rather than netted into pnl. For a retail option
    # buyer the dominant line is usually STT on the sell leg, and you cannot
    # act on that if it is buried inside a single number.
    costs: dict = field(default_factory=dict)
    # Whether the premium was observed in the archive or modelled with
    # Black-Scholes. Until months of snapshots accumulate this reads
    # "modelled" for every trade, and a result that does not say so invites
    # being believed.
    premium_source: str = "modelled"
    # What each check contributed to the decision that opened this trade.
    # Without this you can measure whether the combined score works, but
    # never which part of it is carrying or dragging.
    checks: dict = field(default_factory=dict)

    gross_pnl: float = 0.0
    execution_friction: float = 0.0
    fees: float = 0.0
    timing: dict = field(default_factory=dict)

    # The execution record. The levels here are index levels — what the
    # strategy watches — while the money is premium, and keeping both
    # planned and actual means a reviewer can see which of the two moved.
    entry_side: str = ""
    exit_side: str = ""
    planned_entry: float = 0.0
    actual_entry: float = 0.0
    planned_stop: float = 0.0
    planned_target: float = 0.0
    gap_amount: float = 0.0
    execution_policy: str = execution.KEEP_PLANNED
    ambiguous_intrabar: bool = False
    brokerage: float = 0.0
    statutory_fees: float = 0.0
    net_pnl: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptionBacktestResult:
    trades: list[OptionTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    assumptions: dict = field(default_factory=dict)
    dataset: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "stats": self.stats,
            "assumptions": self.assumptions,
            "dataset": self.dataset,
        }


def next_weekly_expiry(moment: datetime, weekday: int = 1) -> datetime:
    """The next NIFTY weekly expiry at or after `moment`.

    Default is Tuesday, which is where NIFTY weeklies sit at the time of
    writing. NSE has moved this before and will again — pass `weekday`
    explicitly rather than trusting the default across a long backtest.
    """
    days_ahead = (weekday - moment.weekday()) % 7
    candidate = (moment + timedelta(days=days_ahead)).replace(
        hour=15, minute=30, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=7)
    return candidate


def compute_stats(trades: list[OptionTrade], equity: list[float],
                  starting_capital: float) -> dict:
    if not trades:
        return {"trades": 0, "note": "No trades were taken with these rules."}

    pnls = np.array([t.pnl for t in trades])
    rs = np.array([t.r_multiple for t in trades])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    decay = np.array([t.decay_cost for t in trades])

    curve = np.array(equity or [starting_capital])
    peak = np.maximum.accumulate(curve)
    max_dd = float(((curve - peak) / peak).min() * 100)

    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0

    # How often was the direction right but the trade still lost? This is
    # the number that tells you whether decay is eating the edge.
    right_but_lost = sum(
        1 for t in trades
        if t.pnl <= 0 and (
            (t.direction == "BUY" and t.index_exit > t.index_entry)
            or (t.direction == "SELL" and t.index_exit < t.index_entry)
        )
    )

    return {
        "trades": len(trades),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "gross_pnl": round(sum(t.gross_pnl for t in trades), 2),
        "execution_friction": round(sum(t.execution_friction for t in trades), 2),
        "fees_taxes": round(sum(t.fees for t in trades), 2),
        "brokerage": round(sum(t.brokerage for t in trades), 2),
        "statutory_fees": round(sum(t.statutory_fees for t in trades), 2),
        # Trades whose exit was chosen by policy rather than observed.
        "ambiguous_trade_count": sum(1 for t in trades if t.ambiguous_intrabar),
        "gapped_exit_count": sum(1 for t in trades
                                 if t.exit_reason in (execution.STOP_GAP,
                                                      execution.TARGET_GAP)),
        "return_pct": round(float(pnls.sum()) / starting_capital * 100, 2),
        "expectancy_per_trade": round(float(pnls.mean()), 2),
        "expectancy_r": round(float(rs.mean()), 3),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 2),
        # What that drawdown is measured on, stated rather than assumed.
        # Sequential and non-overlapping: this engine holds one position
        # at a time and marks equity when a trade closes. That is not the
        # signal study's overlapping hypothetical outcome curve, which
        # carries its own name for exactly this reason. It is also not a
        # daily mark-to-market portfolio — no capital constraint and no
        # margin model stands behind it.
        "drawdown_basis": (
            "realised_pnl_at_exit; one position at a time; non-overlapping; "
            "no capital constraint; not daily mark-to-market"),
        "avg_decay_cost": round(float(decay.mean()), 2),
        "total_decay_cost": round(float(decay.sum()), 2),
        "right_direction_but_lost": right_but_lost,
        "final_equity": round(float(curve[-1]), 2),
    }


def run(
    candles: pd.DataFrame,
    *,
    starting_capital: float = 200_000,
    risk_config: RiskConfig | None = None,
    iv: float = option_pricing.DEFAULT_IV,
    strike_offset: int = 0,
    expiry_weekday: int = 1,
    lot_size: int = 75,
    cost_per_round_trip: float | None = None,
    slippage_points: float | None = None,
    warmup: int = 60,
    max_bars_in_trade: int = 24,
    analysis_window: int = 300,
    signal_fn: Callable[[pd.DataFrame], signal_engine.Signal] | None = None,
    cost_model: CostModel | FlatCostModel | None = None,
    slippage_model: SlippageModel | None = None,
    execution_policy: ExecutionPolicy | None = None,
    dataset: dict | None = None,
) -> OptionBacktestResult:
    """Walk the candles forward, buying options on each approved signal.

    Costs are itemised rather than flat here, because this is where turnover
    is real. A flat rupee charge has the wrong *shape* for options: the
    statutory charges scale with premium, so one number simultaneously
    overcharges a cheap out-of-the-money trade and undercharges an expensive
    in-the-money one. On a strategy whose edge is a fraction of an R, that
    shape error is enough to flip the sign of the expectancy.

    `cost_per_round_trip` and `slippage_points` are kept as overrides so an
    older result can be reproduced exactly, but they are no longer the
    default. See `backtest/costs.py`.
    """
    feed = HistoricalFeed(candles, analysis_window=analysis_window)
    cfg = (replace(risk_config) if risk_config
           else RiskConfig(capital=starting_capital, lot_size=lot_size))
    signal_fn = signal_fn or (lambda frame: signal_engine.generate(frame))

    costs = cost_model or (
        FlatCostModel(per_round_trip=cost_per_round_trip)
        if cost_per_round_trip is not None else CostModel())
    slip_model = slippage_model or (
        SlippageModel(ticks=slippage_points / costs_module.TICK_SIZE)
        if slippage_points is not None else SlippageModel())
    policy = execution_policy or ExecutionPolicy()
    stamps = feed.stamps()
    rejected_gap_entries = 0

    equity = starting_capital
    curve: list[float] = [equity]
    trades: list[OptionTrade] = []
    day_states: dict[date, DayState] = {}
    open_trade: dict | None = None
    ledger = PositionLedger()
    initial_risk = asdict(cfg)
    custom_signal = signal_fn

    for i in feed.walk(warmup, reserve=0):
        bar = feed.bar(i)
        moment = feed.close_time(i).tz_convert("Asia/Kolkata").to_pydatetime().replace(tzinfo=None)
        state = day_states.setdefault(moment.date(), DayState(trading_day=moment.date()))

        # ---- manage an open position ---------------------------------
        # Never on a bar earlier than the one the order filled on.
        if open_trade and i >= open_trade["entry_index"]:
            years = option_pricing.years_to_expiry(moment, open_trade["expiry"])
            index_now = float(bar["close"])

            index_exit, reason = None, ""
            ambiguous = False
            level_exit = execution.resolve_levels(
                open_trade["direction"], bar_open=float(bar["open"]),
                high=float(bar["high"]), low=float(bar["low"]),
                stop=open_trade["stop"], target=open_trade["target"],
                policy=policy)
            if level_exit is not None:
                index_exit, reason = level_exit.price, level_exit.reason
                ambiguous = level_exit.ambiguous_intrabar
            elif i - open_trade["entry_index"] >= max_bars_in_trade:
                index_exit, reason = index_now, "time"
            elif moment.hour * 60 + moment.minute >= 15 * 60 + 15:
                index_exit, reason = index_now, "session end"

            if index_exit is None and (i == len(feed) - 1 or not feed.can_enter(i)):
                index_exit, reason = float(bar["close"]), (
                    "end_of_data" if i == len(feed)-1 else "session_or_data_boundary")

            if index_exit is not None:
                quoted_exit = option_pricing.price(
                    index_exit, open_trade["strike"], years, iv,
                    kind=open_trade["kind"])
                exit_fill = sell_fill(quoted_exit, slip_model)
                premium_exit = exit_fill.filled

                qty = open_trade["quantity"]
                # The option is always *bought* to open and sold to close,
                # whichever way the index trade was pointed, and the fees
                # fall on the premium legs — never on the index level.
                money = execution.account(
                    entry_side="BUY", entry_price=open_trade["premium_entry"],
                    exit_price=premium_exit, quantity=qty, costs=costs,
                    reference_entry=open_trade["entry_reference"],
                    reference_exit=exit_fill.requested)
                pnl = money.net_pnl

                # What the same index move would have been worth with no
                # time passing — the difference is the decay bill.
                no_decay = option_pricing.price(
                    index_exit, open_trade["strike"], open_trade["entry_years"],
                    iv, kind=open_trade["kind"])
                decay_cost = round((no_decay - premium_exit) * qty, 2)

                risk_amount = open_trade["premium_risk"] * qty
                equity += pnl
                trades.append(OptionTrade(
                    entry_time=open_trade["entry_time"],
                    exit_time=feed.close_time(i).isoformat(),
                    direction=open_trade["direction"],
                    option=f"{open_trade['strike']:.0f} {open_trade['kind']}",
                    strike=open_trade["strike"], kind=open_trade["kind"],
                    index_entry=round(open_trade["index_entry"], 2),
                    index_exit=round(index_exit, 2),
                    premium_entry=round(open_trade["premium_entry"], 2),
                    premium_exit=round(premium_exit, 2),
                    lots=open_trade["lots"], quantity=qty,
                    pnl=round(pnl, 2),
                    r_multiple=round(pnl / risk_amount, 3) if risk_amount else 0.0,
                    decay_cost=decay_cost,
                    exit_reason=reason,
                    confidence=open_trade["confidence"],
                    checks=open_trade["checks"],
                    costs=money.breakdown,
                    premium_source="modelled",
                    gross_pnl=round(money.gross_pnl, 2),
                    execution_friction=round(money.execution_friction, 2),
                    fees=round(money.total_fees, 2),
                    timing=open_trade["timing"],
                    entry_side=money.entry_side, exit_side=money.exit_side,
                    planned_entry=round(open_trade["planned_entry"], 2),
                    actual_entry=round(open_trade["index_entry"], 2),
                    planned_stop=round(open_trade["planned_stop"], 2),
                    planned_target=round(open_trade["planned_target"], 2),
                    gap_amount=round(open_trade["gap_amount"], 4),
                    execution_policy=open_trade["execution_policy"],
                    ambiguous_intrabar=ambiguous,
                    brokerage=round(money.brokerage, 2),
                    statutory_fees=round(money.statutory_fees, 2),
                    net_pnl=round(money.net_pnl, 2),
                ))
                pnl = round(pnl, 2)
                # Account balances reconcile exactly to the reported monetary ledger.
                equity = starting_capital + sum(t.pnl for t in trades)
                ledger.close(feed.close_time(i), reason, pnl)
                state.record_close(pnl)
                curve.append(equity)
                open_trade = None

        if open_trade or not feed.can_enter(i):
            continue

        # ---- look for a new entry ------------------------------------
        try:
            sig = signal_fn(feed.view(i))
        except Exception:
            continue
        if sig.action == "HOLD" or sig.stop_loss is None:
            continue

        expiry = next_weekly_expiry(moment, expiry_weekday)
        years = option_pricing.years_to_expiry(moment, expiry)
        if years <= 0:
            continue

        signal_time = feed.close_time(i)
        earliest = execution.earliest_execution_time(signal_time, policy)
        exec_index = execution.first_executable_index(stamps, i, signal_time, policy)
        if exec_index is None:
            continue
        fill_stamp, entry_index = feed.execution_open(i, exec_index)

        # The signal's stop was computed on this bar's close, but the fill
        # happens at the next executable bar's open. Overnight and lunch
        # gaps can move price straight past the stop, leaving it on the
        # wrong side of the entry. That is not a trade with tiny risk — it
        # is a trade whose premise is already invalid, and it is refused
        # rather than sized. Clamping the risk to a small floor instead
        # turned one such trade into 654 lots and a fictional 473% return.
        #
        # The refusal now comes from the shared policy, so the index
        # backtest and this one agree on which gaps are fatal instead of
        # one skipping and the other quietly moving the levels.
        entry_plan = execution.plan_entry(
            sig.action, planned_entry=sig.entry, planned_stop=sig.stop_loss,
            planned_target=sig.target, actual_entry=entry_index, policy=policy)
        if not entry_plan.accepted:
            rejected_gap_entries += 1
            continue

        strike, kind = option_pricing.select_strike(
            entry_index, sig.action, offset=strike_offset)

        quoted_entry = option_pricing.price(entry_index, strike, years, iv, kind=kind)
        entry_fill = buy_fill(quoted_entry, slip_model)
        entry_premium = entry_fill.filled
        if entry_premium <= 0:
            continue

        # Risk in premium terms: what the option would be worth if the index
        # reached the stop. This is the correction that matters — sizing off
        # index points overstates risk by roughly the inverse of delta.
        premium_at_stop = option_pricing.price(
            entry_plan.stop, strike, years, iv, kind=kind)
        premium_risk = entry_premium - premium_at_stop

        # No silent floor. A risk that is negative, zero, or a trivial
        # fraction of the premium means the pricing does not agree with the
        # signal's levels, and dividing by it produces absurd position
        # sizes. Skip and say nothing rather than invent a trade.
        if premium_risk < entry_premium * 0.02:
            continue
        premium_at_target = option_pricing.price(
            entry_plan.target, strike, years, iv, kind=kind)

        cfg.capital = equity
        decision = evaluate(
            config=cfg, state=state,
            entry=entry_premium,
            stop_loss=entry_premium - premium_risk,
            target=entry_premium + (premium_at_target - entry_premium),
            unit_cost=entry_premium,
        )
        if not decision.approved:
            continue

        open_trade = {
            "entry_reference": entry_fill.requested, "entry_friction": entry_fill.slippage,
            "timing": {"bar_open_time": feed.timestamp(i).isoformat(),
                       "bar_close_time": signal_time.isoformat(),
                       "signal_time": signal_time.isoformat(),
                       "earliest_execution_time": earliest.isoformat(),
                       "actual_fill_time": fill_stamp.isoformat(),
                       "execution_latency_seconds": policy.latency_seconds},
            "direction": sig.action, "strike": strike, "kind": kind,
            "expiry": expiry, "entry_years": years,
            "index_entry": entry_index,
            "premium_entry": entry_premium,
            "premium_risk": premium_risk,
            "stop": entry_plan.stop, "target": entry_plan.target,
            "planned_entry": entry_plan.planned_entry,
            "planned_stop": entry_plan.planned_stop,
            "planned_target": entry_plan.planned_target,
            "gap_amount": entry_plan.gap_amount,
            "execution_policy": entry_plan.execution_policy,
            "quantity": decision.quantity, "lots": decision.lots,
            "entry_index": exec_index,
            "entry_time": fill_stamp.isoformat(),
            "confidence": sig.confidence,
            "checks": {c.name: round(c.contribution, 4) for c in sig.checks},
        }
        ledger.enter(signal_time, fill_stamp, decision.quantity)
        state.record_fill()

    return OptionBacktestResult(
        trades, curve,
        compute_stats(trades, curve, starting_capital),
        assumptions=describe(costs, slip_model) | {
            "iv": iv,
            "iv_source": "constant — no stored option history to read it from",
            "strike_offset": strike_offset,
            "expiry_weekday": expiry_weekday,
            "lot_size": lot_size,
            "premium_source": "modelled",
            "warmup_bars": warmup,
            "max_bars_in_trade": max_bars_in_trade,
            "execution": policy.describe(),
            "entries_rejected_due_to_gap": rejected_gap_entries,
        },
        dataset=(dataset or {}) | {"reproducibility": provenance(candles,
            {"risk": initial_risk, "costs": describe(costs, slip_model),
             "execution": policy.describe(),
             "warmup": warmup, "analysis_window": analysis_window,
             "max_bars_in_trade": max_bars_in_trade}, custom_signal),
             "positions": ledger.finish(trades, starting_capital, equity)},
    )
