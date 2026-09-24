"""What the run turned out to be, cut the ways that change a decision.

A headline expectancy is one number over a mixed bag. Two of the cuts below
exist because option buying fails in ways an index result cannot express:

  **Expiry distance.** The same setup two days from expiry and seven days
  from expiry are different trades. If the edge only appears in one bucket,
  the strategy is a tenor bet wearing a signal's clothes.

  **Time of day.** Decay is not linear across a session and neither is
  liquidity. A result carried entirely by the first half hour is a result
  about the opening auction.

And one that exists because of what this platform is:

  **Evidence.** Trades priced from the archive and trades priced by
  Black-Scholes are grouped apart, always. If the observed bucket and the
  modelled bucket disagree, the model is the thing being measured — and
  averaging them would have hidden exactly that.

Every bucket carries its own count. A 100% win rate over two trades is not a
finding, and the count is what stops it being read as one.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

import numpy as np

from ..market_hours import IST

# Sessions run 09:15–15:30 IST. The edges are named for what happens in
# them, not split into equal blocks: the opening auction and the last half
# hour behave differently from the middle and always have.
TIME_BUCKETS = (
    ("09:15-10:00 open", 9 * 60 + 15, 10 * 60),
    ("10:00-12:00 morning", 10 * 60, 12 * 60),
    ("12:00-14:00 midday", 12 * 60, 14 * 60),
    ("14:00-15:30 close", 14 * 60, 15 * 60 + 30),
)

EXPIRY_BUCKETS = (
    ("0-1d", 0.0, 1.0),
    ("1-2d", 1.0, 2.0),
    ("2-4d", 2.0, 4.0),
    ("4-7d", 4.0, 7.0),
    ("7d+", 7.0, float("inf")),
)

CONFIDENCE_BUCKETS = (
    ("<50%", 0.0, 0.50),
    ("50-60%", 0.50, 0.60),
    ("60-70%", 0.60, 0.70),
    ("70-80%", 0.70, 0.80),
    ("80%+", 0.80, float("inf")),
)


def _bucket_of(value: float | None, buckets) -> str:
    if value is None:
        return "unknown"
    for name, low, high in buckets:
        if low <= value < high:
            return name
    return buckets[-1][0]


def _minutes_ist(iso: str) -> int | None:
    """Minutes past midnight IST for an ISO timestamp, or None.

    The stamps are UTC-aware, so the conversion is arithmetic rather than a
    guess — and a naive one would file a 09:20 IST entry under 03:50.
    """
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    local = moment.astimezone(IST)
    return local.hour * 60 + local.minute


def stats(trades: Sequence, curve: Sequence[float],
          starting_capital: float) -> dict:
    """The headline numbers, all after costs.

    Costs are reported alongside rather than only netted in. For a retail
    option buyer the charges are a material share of a small edge, and a
    result that only shows the net figure cannot answer "would this work at
    a different brokerage?".
    """
    if not trades:
        return {"trades": 0,
                "note": "No trades were taken with these rules. Read the "
                        "rejection counts to see what stopped them."}

    pnls = np.array([t.pnl for t in trades], dtype=float)
    gross = np.array([t.gross_pnl for t in trades], dtype=float)
    rs = np.array([t.r_multiple for t in trades], dtype=float)
    charges = np.array([t.costs.get("total", 0.0) for t in trades], dtype=float)
    friction = np.array([t.execution_friction for t in trades], dtype=float)
    decay = np.array([t.decay_cost for t in trades], dtype=float)

    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    equity = np.array(list(curve) or [starting_capital], dtype=float)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak

    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0

    # Directionally right and still red. This is the number that says decay
    # rather than the signal is what cost the money.
    right_but_lost = sum(
        1 for t in trades
        if t.pnl <= 0 and ((t.direction == "BUY" and t.index_exit > t.index_entry)
                           or (t.direction == "SELL" and t.index_exit < t.index_entry)))

    return {
        "trades": len(trades),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "net_pnl": round(float(pnls.sum()), 2),
        "gross_pnl": round(float(gross.sum()), 2),
        "fees_taxes": round(float(charges.sum()), 2),
        "brokerage": round(float(sum(t.brokerage for t in trades)), 2),
        "statutory_fees": round(float(sum(t.statutory_fees for t in trades)), 2),
        "execution_friction": round(float(friction.sum()), 2),
        # The same friction, split by cause. A quoted spread is measured and
        # a sensitivity sweep cannot move it; the impact on top of it is an
        # assumption and is the only part that scales. One combined figure
        # made a flat sweep over a real book look like a finding.
        "spread_cost": round(float(sum(t.spread_cost for t in trades)), 2),
        "impact_cost": round(float(sum(t.impact_cost for t in trades)), 2),
        "total_costs": round(float(charges.sum() + friction.sum()), 2),
        # How many of these outcomes were chosen by the stop-first rule
        # rather than observed, and how many exits filled away from their
        # level because the bar gapped through it. Both are properties of
        # the sample and belong beside the win rate, not in a footnote.
        "ambiguous_trade_count": sum(1 for t in trades if t.ambiguous_intrabar),
        "gapped_exit_count": sum(
            1 for t in trades if t.exit_reason in ("stop_gap", "target_gap")),
        "cost_per_trade": round(float(charges.mean()), 2),
        "return_pct": round(float(pnls.sum()) / starting_capital * 100, 2),
        "expectancy_per_trade": round(float(pnls.mean()), 2),
        "expectancy_r": round(float(rs.mean()), 3),
        # The same expectancy with the charges added back. The gap between
        # these two is the cost drag, and it is the number that answers
        # whether a thin edge survives a different brokerage.
        "expectancy_per_trade_before_costs": round(float(gross.mean()), 2),
        "expectancy_r_before_costs": round(float(np.mean([
            (t.gross_pnl / t.risk_amount) if t.risk_amount else 0.0
            for t in trades])), 3),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 2),
        "max_drawdown_value": round(float((equity - peak).min()), 2),
        # What that drawdown is measured on, stated rather than assumed.
        # This run holds one position at a time and marks equity when a
        # trade closes, so the curve is sequential and non-overlapping —
        # unlike the signal study's overlapping outcome curve, which is a
        # different thing and carries a different name. It is still not a
        # daily mark-to-market portfolio: nothing constrains the capital
        # and no margin model stands behind it.
        "drawdown_basis": (
            "realised_pnl_at_exit; one position at a time; non-overlapping; "
            "no capital constraint; not daily mark-to-market"),
        "avg_decay_cost": round(float(decay.mean()), 2),
        "total_decay_cost": round(float(decay.sum()), 2),
        "right_direction_but_lost": right_but_lost,
        "avg_bars_held": round(float(np.mean([t.bars_held for t in trades])), 1),
        "final_equity": round(float(equity[-1]), 2),
    }


def bucket(trades: Sequence, key: Callable[[object], str]) -> list[dict]:
    """One row per group, with the count that decides whether to read it."""
    groups: dict[str, list] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade)

    rows = []
    for name, group in groups.items():
        pnls = np.array([t.pnl for t in group], dtype=float)
        rs = np.array([t.r_multiple for t in group], dtype=float)
        wins = int((pnls > 0).sum())
        rows.append({
            "bucket": name,
            "trades": len(group),
            "wins": wins,
            "win_rate_pct": round(wins / len(group) * 100, 1),
            "net_pnl": round(float(pnls.sum()), 2),
            "expectancy_r": round(float(rs.mean()), 3),
            "total_costs": round(
                float(sum(t.costs.get("total", 0.0) for t in group)), 2),
            "note": ("Too few trades to read." if len(group) < 10 else None),
        })
    return sorted(rows, key=lambda r: -r["trades"])


def breakdowns(trades: Sequence) -> dict:
    """Every cut the strategy is judged by, in one block."""
    if not trades:
        return {}
    return {
        "by_regime_day": bucket(trades, lambda t: t.regime_day or "unknown"),
        "by_regime_hour": bucket(trades, lambda t: t.regime_hour or "unknown"),
        "by_time_of_day": bucket(
            trades, lambda t: _bucket_of(_minutes_ist(t.entry_time), TIME_BUCKETS)),
        "by_expiry_distance": bucket(
            trades, lambda t: _bucket_of(t.days_to_expiry, EXPIRY_BUCKETS)),
        "by_confidence": bucket(
            trades, lambda t: _bucket_of(t.confidence, CONFIDENCE_BUCKETS)),
        "by_evidence": bucket(trades, lambda t: t.evidence),
        "by_exit_reason": bucket(trades, lambda t: t.exit_reason),
        "by_option_type": bucket(trades, lambda t: t.option_type),
        "by_moneyness": bucket(trades, lambda t: t.moneyness),
        "by_bias": bucket(trades, lambda t: t.bias or "unknown"),
    }
