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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from ..analytics import option_pricing, signal_engine
from ..risk.manager import DayState, RiskConfig, evaluate
from . import costs as costs_module
from .costs import CostModel, FlatCostModel, SlippageModel, buy_fill, describe, sell_fill
from .feed import HistoricalFeed


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
        "return_pct": round(float(pnls.sum()) / starting_capital * 100, 2),
        "expectancy_per_trade": round(float(pnls.mean()), 2),
        "expectancy_r": round(float(rs.mean()), 3),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 2),
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
    cfg = risk_config or RiskConfig(capital=starting_capital, lot_size=lot_size)
    signal_fn = signal_fn or (lambda frame: signal_engine.generate(frame))

    costs = cost_model or (
        FlatCostModel(per_round_trip=cost_per_round_trip)
        if cost_per_round_trip is not None else CostModel())
    slip_model = slippage_model or (
        SlippageModel(ticks=slippage_points / costs_module.TICK_SIZE)
        if slippage_points is not None else SlippageModel())

    equity = starting_capital
    curve: list[float] = [equity]
    trades: list[OptionTrade] = []
    day_states: dict[date, DayState] = {}
    open_trade: dict | None = None

    for i in feed.walk(warmup):
        bar = feed.bar(i)
        moment = feed.ist(i).to_pydatetime().replace(tzinfo=None)
        state = day_states.setdefault(moment.date(), DayState(trading_day=moment.date()))

        # ---- manage an open position ---------------------------------
        if open_trade:
            years = option_pricing.years_to_expiry(moment, open_trade["expiry"])
            index_now = float(bar["close"])

            hit_stop = (bar["low"] <= open_trade["stop"]) if open_trade["direction"] == "BUY" \
                else (bar["high"] >= open_trade["stop"])
            hit_target = (bar["high"] >= open_trade["target"]) if open_trade["direction"] == "BUY" \
                else (bar["low"] <= open_trade["target"])

            index_exit, reason = None, ""
            if hit_stop:                      # pessimistic: stop fills first
                index_exit, reason = open_trade["stop"], "stop"
            elif hit_target:
                index_exit, reason = open_trade["target"], "target"
            elif i - open_trade["entry_index"] >= max_bars_in_trade:
                index_exit, reason = index_now, "time"
            elif feed.ist(i).time().hour >= 15 and feed.ist(i).time().minute >= 15:
                index_exit, reason = index_now, "session end"

            if index_exit is not None:
                quoted_exit = option_pricing.price(
                    index_exit, open_trade["strike"], years, iv,
                    kind=open_trade["kind"])
                exit_fill = sell_fill(quoted_exit, slip_model)
                premium_exit = exit_fill.filled

                qty = open_trade["quantity"]
                gross = (premium_exit - open_trade["premium_entry"]) * qty
                charges = costs.round_trip(
                    buy_price=open_trade["premium_entry"],
                    sell_price=premium_exit,
                    quantity=qty)
                pnl = gross - charges.total

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
                    exit_time=feed.timestamp(i).isoformat(),
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
                    costs=charges.to_dict(),
                    premium_source="modelled",
                ))
                state.record_close(pnl)
                curve.append(equity)
                open_trade = None

        if open_trade:
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

        entry_index = feed.next_open(i)

        # The signal's stop was computed on this bar's close, but the fill
        # happens at the next bar's open. Overnight and lunch gaps can move
        # price straight past the stop, leaving it on the wrong side of the
        # entry. That is not a trade with tiny risk — it is a trade whose
        # premise is already invalid, and it must be skipped rather than
        # sized. Clamping the risk to a small floor instead turned one such
        # trade into 654 lots and a fictional 473% return.
        if sig.action == "BUY" and entry_index <= sig.stop_loss:
            continue
        if sig.action == "SELL" and entry_index >= sig.stop_loss:
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
            sig.stop_loss, strike, years, iv, kind=kind)
        premium_risk = entry_premium - premium_at_stop

        # No silent floor. A risk that is negative, zero, or a trivial
        # fraction of the premium means the pricing does not agree with the
        # signal's levels, and dividing by it produces absurd position
        # sizes. Skip and say nothing rather than invent a trade.
        if premium_risk < entry_premium * 0.02:
            continue
        premium_at_target = option_pricing.price(
            sig.target, strike, years, iv, kind=kind)

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
            "direction": sig.action, "strike": strike, "kind": kind,
            "expiry": expiry, "entry_years": years,
            "index_entry": entry_index,
            "premium_entry": entry_premium,
            "premium_risk": premium_risk,
            "stop": sig.stop_loss, "target": sig.target,
            "quantity": decision.quantity, "lots": decision.lots,
            "entry_index": i + 1,
            "entry_time": feed.next_timestamp(i).isoformat(),
            "confidence": sig.confidence,
            "checks": {c.name: round(c.contribution, 4) for c in sig.checks},
        }
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
        },
        dataset=dataset or {},
    )
