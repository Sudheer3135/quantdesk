"""Backtest engine.

Bar-by-bar, no lookahead. At bar i the engine may only see candles 0..i.
Entries fill at the next bar's open, which is the earliest a real order
could realistically be placed after a signal on a closed candle.

Costs are charged on both legs so the equity curve is net, not gross.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..analytics import indicators, signal_engine
from ..risk.manager import DayState, RiskConfig, evaluate


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

    def to_dict(self) -> dict:
        return {
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "stats": self.stats,
        }


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

    returns = np.diff(curve) / curve[:-1] if len(curve) > 1 else np.array([0.0])
    sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252)) \
        if len(returns) > 1 and np.std(returns) > 0 else 0.0
    downside = returns[returns < 0]
    sortino = float(np.mean(returns) / np.std(downside) * np.sqrt(252)) \
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
) -> BacktestResult:
    """Walk the candles forward and simulate the rulebook.

    cost_per_round_trip is a flat rupee charge covering brokerage, STT,
    exchange fees, GST and stamp duty for both legs. A flat figure is used
    rather than a percentage of index turnover because in Indian F&O the
    charges scale with premium and lot count, not with the index level.
    Replace it with your own number from a real contract note.

    analysis_window caps how many bars each signal call sees. Structure
    detection is O(n) per call, so an uncapped window makes the whole
    backtest O(n squared). 300 bars is four sessions of 5-minute data,
    which is more history than any of the checks actually use.
    """
    df = indicators.enrich(candles)
    cfg = risk_config or RiskConfig(capital=starting_capital)
    signal_fn = signal_fn or (lambda frame: signal_engine.generate(frame))

    equity = starting_capital
    curve: list[float] = [equity]
    trades: list[Trade] = []

    day_states: dict[date, DayState] = {}
    open_trade: dict | None = None

    ist = df["timestamp"].dt.tz_convert("Asia/Kolkata")

    for i in range(warmup, len(df) - 1):
        bar = df.iloc[i]
        nxt = df.iloc[i + 1]
        today = ist.iloc[i].date()
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
            elif ist.iloc[i].time().hour >= 15 and ist.iloc[i].time().minute >= 15:
                exit_price, reason = float(bar["close"]), "session end"

            if exit_price is not None:
                qty = open_trade["quantity"]
                direction = 1 if open_trade["side"] == "BUY" else -1
                slip = exit_price * slippage_pct / 100 * direction
                fill = exit_price - slip
                gross = (fill - open_trade["entry"]) * direction * qty
                pnl = gross - cost_per_round_trip
                equity += pnl
                risk_unit = abs(open_trade["entry"] - open_trade["stop"]) * qty
                trades.append(Trade(
                    entry_time=open_trade["entry_time"],
                    exit_time=bar["timestamp"].isoformat(),
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
        start = max(0, i + 1 - analysis_window)
        window = df.iloc[start : i + 1]
        try:
            sig = signal_fn(window)
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
        entry_fill = float(nxt["open"]) + float(nxt["open"]) * slippage_pct / 100 * direction
        shift = entry_fill - sig.entry
        open_trade = {
            "side": sig.action,
            "entry": entry_fill,
            "stop": sig.stop_loss + shift,
            "target": sig.target + shift,
            "quantity": decision.quantity,
            "entry_index": i + 1,
            "entry_time": nxt["timestamp"].isoformat(),
            "confidence": sig.confidence,
        }
        state.record_fill()

    return BacktestResult(trades, curve, compute_stats(trades, curve, starting_capital))
