"""Backtest engine.

Bar-by-bar, no lookahead. At bar i the engine may only see candles 0..i.
Entries fill at the next bar's open, which is the earliest a real order
could realistically be placed after a signal on a closed candle.

That rule used to be a convention this file was careful about. It is now
enforced by `HistoricalFeed`, which will not hand over a bar the walk has
not reached and returns the next bar's *open alone* rather than the whole
row. See `backtest/feed.py` for why one number instead of one row matters.

Costs are charged on both legs so the equity curve is net, not gross.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..analytics import signal_engine
from ..risk.manager import DayState, RiskConfig, evaluate
from .costs import CostModel, FlatCostModel, SlippageModel, describe
from .feed import HistoricalFeed


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    side: str
    entry: float
    exit: float
    quantity: int
    stop_loss: float
    target: float
    pnl: float
    r_multiple: float
    exit_reason: str
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    # What the run assumed and what it read. A result without these cannot
    # be compared against another result, because nothing records whether
    # the difference was the strategy, the costs, or the data.
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


def _annualisation(trades: list[Trade]) -> tuple[float, str]:
    """How many trade-returns a year, for annualising a trade-indexed curve.

    Returns the scaling factor and a plain description of where it came
    from, because a Sharpe ratio whose basis is undocumented invites being
    compared against numbers computed on a completely different one.

    A single trade, or trades spanning less than a week, cannot support an
    annual figure at all — the honest answer there is to refuse to scale
    rather than to extrapolate one week into a year.
    """
    if len(trades) < 2:
        return 1.0, "too few trades to annualise"

    first = pd.Timestamp(trades[0].entry_time)
    last = pd.Timestamp(trades[-1].exit_time)
    days = (last - first).total_seconds() / 86400
    if days < 7:
        return 1.0, f"span of {days:.1f} days is too short to annualise"

    years = days / 365.25
    per_year = len(trades) / years
    return per_year, (
        f"{len(trades)} trades over {days:.0f} days "
        f"({per_year:.0f} trades/year)")


def compute_stats(trades: list[Trade], equity: list[float], starting_capital: float) -> dict:
    if not trades:
        return {"trades": 0, "note": "No trades were taken with these rules."}

    pnls = np.array([t.pnl for t in trades])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    rs = np.array([t.r_multiple for t in trades])

    curve = np.array(equity or [starting_capital])
    peak = np.maximum.accumulate(curve)
    drawdown = (curve - peak) / peak
    max_dd = float(drawdown.min() * 100)

    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0

    # Streaks
    best_win_streak = worst_loss_streak = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        best_win_streak = max(best_win_streak, cur_w)
        worst_loss_streak = max(worst_loss_streak, cur_l)

    # The equity curve gains a point per *trade*, not per day — nothing is
    # appended on a bar where nothing closed. Annualising those returns by
    # √252 therefore treated every trade as if it took exactly one day,
    # which inflates the ratio for a strategy holding 20 minutes and
    # deflates it for one holding a week. Either way the published number
    # was not comparable to anything, including its own previous runs.
    #
    # The fix is to scale by the rate trades actually arrived at.
    returns = np.diff(curve) / curve[:-1] if len(curve) > 1 else np.array([0.0])
    periods_per_year, basis = _annualisation(trades)

    sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(periods_per_year)) \
        if len(returns) > 1 and np.std(returns) > 0 else 0.0
    downside = returns[returns < 0]
    sortino = float(np.mean(returns) / np.std(downside) * np.sqrt(periods_per_year)) \
        if len(downside) > 1 and np.std(downside) > 0 else 0.0

    return {
        "trades": len(trades),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "return_pct": round(float(pnls.sum()) / starting_capital * 100, 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "largest_win": round(float(pnls.max()), 2),
        "largest_loss": round(float(pnls.min()), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "expectancy_per_trade": round(float(pnls.mean()), 2),
        "expectancy_r": round(float(rs.mean()), 3),
        "avg_r_win": round(float(rs[rs > 0].mean()), 3) if (rs > 0).any() else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        # Without this, the ratio above is a number with no units. Two runs
        # over different holding periods produce Sharpes that cannot be
        # compared, and nothing in the output would say so.
        "sharpe_basis": basis,
        "best_win_streak": best_win_streak,
        "worst_loss_streak": worst_loss_streak,
        "final_equity": round(float(curve[-1]), 2),
    }


def run(
    candles: pd.DataFrame,
    *,
    starting_capital: float = 100_000,
    risk_config: RiskConfig | None = None,
    warmup: int = 60,
    cost_per_round_trip: float = 120.0,
    slippage_pct: float = 0.02,
    signal_fn: Callable[[pd.DataFrame], signal_engine.Signal] | None = None,
    max_bars_in_trade: int = 24,
    analysis_window: int = 300,
    cost_model: CostModel | FlatCostModel | None = None,
    slippage_model: SlippageModel | None = None,
    dataset: dict | None = None,
) -> BacktestResult:
    """Walk the candles forward and simulate the rulebook.

    Costs default to a flat rupee charge per round trip, which is the right
    shape here and only here: this engine trades index points, so there is
    no premium turnover to charge a percentage against. The option engine,
    where turnover is real, uses the itemised `CostModel` instead. Pass
    `cost_model` to override either.

    analysis_window caps how many bars each signal call sees. Structure
    detection is O(n) per call, so an uncapped window makes the whole
    backtest O(n squared). 300 bars is four sessions of 5-minute data,
    which is more history than any of the checks actually use.
    """
    feed = HistoricalFeed(candles, analysis_window=analysis_window)
    cfg = risk_config or RiskConfig(capital=starting_capital)
    signal_fn = signal_fn or (lambda frame: signal_engine.generate(frame))
    costs = cost_model or FlatCostModel(per_round_trip=cost_per_round_trip)
    slip_model = slippage_model or SlippageModel(index_pct=slippage_pct)

    equity = starting_capital
    curve: list[float] = [equity]
    trades: list[Trade] = []

    day_states: dict[date, DayState] = {}
    open_trade: dict | None = None

    for i in feed.walk(warmup):
        bar = feed.bar(i)
        moment = feed.ist(i)
        today = moment.date()
        state = day_states.setdefault(today, DayState(trading_day=today))

        # ---- manage an open trade on this bar -------------------------
        if open_trade:
            hit_stop = (bar["low"] <= open_trade["stop"]) if open_trade["side"] == "BUY" \
                else (bar["high"] >= open_trade["stop"])
            hit_target = (bar["high"] >= open_trade["target"]) if open_trade["side"] == "BUY" \
                else (bar["low"] <= open_trade["target"])

            exit_price, reason = None, ""
            # If both are touched inside one candle, assume the stop filled
            # first. Pessimistic on purpose — never flatter the backtest.
            if hit_stop:
                exit_price, reason = open_trade["stop"], "stop"
            elif hit_target:
                exit_price, reason = open_trade["target"], "target"
            elif i - open_trade["entry_index"] >= max_bars_in_trade:
                exit_price, reason = float(bar["close"]), "time"
            elif moment.time().hour >= 15 and moment.time().minute >= 15:
                exit_price, reason = float(bar["close"]), "session end"

            if exit_price is not None:
                qty = open_trade["quantity"]
                direction = 1 if open_trade["side"] == "BUY" else -1
                slip = slip_model.index_points(exit_price) * direction
                fill = exit_price - slip
                gross = (fill - open_trade["entry"]) * direction * qty
                charges = costs.round_trip(
                    buy_price=min(open_trade["entry"], fill),
                    sell_price=max(open_trade["entry"], fill),
                    quantity=qty)
                pnl = gross - charges.total
                equity += pnl
                risk_unit = abs(open_trade["entry"] - open_trade["stop"]) * qty
                trades.append(Trade(
                    entry_time=open_trade["entry_time"],
                    exit_time=feed.timestamp(i).isoformat(),
                    side=open_trade["side"],
                    entry=round(open_trade["entry"], 2),
                    exit=round(fill, 2),
                    quantity=qty,
                    stop_loss=round(open_trade["stop"], 2),
                    target=round(open_trade["target"], 2),
                    pnl=round(pnl, 2),
                    r_multiple=round(pnl / risk_unit, 3) if risk_unit else 0.0,
                    exit_reason=reason,
                    confidence=open_trade["confidence"],
                ))
                state.record_close(pnl)
                curve.append(equity)
                open_trade = None

        if open_trade:
            continue

        # ---- look for a new entry using only bars up to i -------------
        # `feed.view(i)` cannot return bar i+1 or later. That is the whole
        # guarantee this engine rests on.
        try:
            sig = signal_fn(feed.view(i))
        except Exception:
            continue
        if sig.action == "HOLD" or sig.stop_loss is None:
            continue

        cfg.capital = equity
        decision = evaluate(
            config=cfg, state=state,
            entry=sig.entry, stop_loss=sig.stop_loss, target=sig.target,
        )
        if not decision.approved:
            continue

        direction = 1 if sig.action == "BUY" else -1
        # The only permitted look forward, and it is one number wide.
        next_open = feed.next_open(i)
        entry_fill = next_open + slip_model.index_points(next_open) * direction
        shift = entry_fill - sig.entry
        open_trade = {
            "side": sig.action,
            "entry": entry_fill,
            "stop": sig.stop_loss + shift,
            "target": sig.target + shift,
            "quantity": decision.quantity,
            "entry_index": i + 1,
            "entry_time": feed.next_timestamp(i).isoformat(),
            "confidence": sig.confidence,
        }
        state.record_fill()

    return BacktestResult(
        trades, curve,
        compute_stats(trades, curve, starting_capital),
        assumptions=describe(costs, slip_model) | {
            "warmup_bars": warmup,
            "analysis_window": analysis_window,
            "max_bars_in_trade": max_bars_in_trade,
            "stop_fills_first_when_both_touched": True,
        },
        dataset=dataset or {},
    )
