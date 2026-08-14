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

from ..analytics import indicators, option_pricing, signal_engine
from ..risk.manager import DayState, RiskConfig, evaluate


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

    def to_dict(self) -> dict:
        return {
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "stats": self.stats,
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
    cost_per_round_trip: float = 120.0,
    slippage_points: float = 0.5,
    warmup: int = 60,
    max_bars_in_trade: int = 24,
    analysis_window: int = 300,
    signal_fn: Callable[[pd.DataFrame], signal_engine.Signal] | None = None,
) -> OptionBacktestResult:
    """Walk the candles forward, buying options on each approved signal.

    `slippage_points` is charged on the premium, not the index — a couple of
    ticks on an option is a far larger fraction of its price than the same
    slippage on a 24,000-point index.
    """
    df = indicators.enrich(candles)
    cfg = risk_config or RiskConfig(capital=starting_capital, lot_size=lot_size)
    signal_fn = signal_fn or (lambda frame: signal_engine.generate(frame))

    equity = starting_capital
    curve: list[float] = [equity]
    trades: list[OptionTrade] = []
    day_states: dict[date, DayState] = {}
    open_trade: dict | None = None

    ist = df["timestamp"].dt.tz_convert("Asia/Kolkata")

    for i in range(warmup, len(df) - 1):
        bar, nxt = df.iloc[i], df.iloc[i + 1]
        moment = ist.iloc[i].to_pydatetime().replace(tzinfo=None)
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
            elif ist.iloc[i].time().hour >= 15 and ist.iloc[i].time().minute >= 15:
                index_exit, reason = index_now, "session end"

            if index_exit is not None:
                premium_exit = option_pricing.price(
                    index_exit, open_trade["strike"], years, iv,
                    kind=open_trade["kind"]) - slippage_points
                premium_exit = max(premium_exit, 0.0)

                qty = open_trade["quantity"]
                gross = (premium_exit - open_trade["premium_entry"]) * qty
                pnl = gross - cost_per_round_trip

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
                    exit_time=bar["timestamp"].isoformat(),
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
                ))
                state.record_close(pnl)
                curve.append(equity)
                open_trade = None

        if open_trade:
            continue

        # ---- look for a new entry ------------------------------------
        window = df.iloc[max(0, i + 1 - analysis_window) : i + 1]
        try:
            sig = signal_fn(window)
        except Exception:
            continue
        if sig.action == "HOLD" or sig.stop_loss is None:
            continue

        expiry = next_weekly_expiry(moment, expiry_weekday)
        years = option_pricing.years_to_expiry(moment, expiry)
        if years <= 0:
            continue

        entry_index = float(nxt["open"])

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

        entry_premium = option_pricing.price(
            entry_index, strike, years, iv, kind=kind) + slippage_points
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
            "entry_time": nxt["timestamp"].isoformat(),
            "confidence": sig.confidence,
            "checks": {c.name: round(c.contribution, 4) for c in sig.checks},
        }
        state.record_fill()

    return OptionBacktestResult(trades, curve,
                                compute_stats(trades, curve, starting_capital))