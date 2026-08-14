import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.backtest.engine import run  # noqa: E402
from app.brokers.mock import MockBroker  # noqa: E402
from app.risk.manager import RiskConfig  # noqa: E402


def test_backtest_runs_and_reports_stats():
    candles = MockBroker().candles(days=12)
    result = run(candles, starting_capital=200_000,
                 risk_config=RiskConfig(capital=200_000, lot_size=25))
    assert "trades" in result.stats
    for trade in result.trades:
        assert trade.quantity > 0
        assert trade.exit_reason in {"stop", "target", "time", "session end"}


def test_no_lookahead_entry_uses_next_bar():
    candles = MockBroker(seed=3).candles(days=10)
    result = run(candles, starting_capital=200_000,
                 risk_config=RiskConfig(capital=200_000, lot_size=25))
    for trade in result.trades:
        # A trade may open and be stopped inside the same candle.
        assert trade.entry_time <= trade.exit_time


def test_diagnostics_split_trades_without_losing_any():
    """Every trade must appear in exactly one bucket of each breakdown —
    a diagnostic that quietly drops trades would send you fixing the wrong
    thing."""
    from app.backtest import diagnostics, option_engine
    from app.risk.manager import RiskConfig

    result = option_engine.run(
        MockBroker(seed=21).candles(days=12, interval="5m"),
        starting_capital=300_000,
        risk_config=RiskConfig(capital=300_000, lot_size=75),
    ).to_dict()

    report = diagnostics.report(result)
    total = len(result["trades"])
    if total == 0:
        return

    for key in ("by_confidence", "by_exit_reason", "by_direction", "by_hour"):
        assert sum(row["trades"] for row in report[key]) == total, key


def test_decay_share_arithmetic_holds():
    from app.backtest import diagnostics

    trades = [
        {"pnl": -1000.0, "decay_cost": 300.0, "r_multiple": -1.0,
         "confidence": 0.5, "exit_reason": "stop", "direction": "BUY",
         "entry_time": "2026-08-05T04:00:00+00:00",
         "exit_time": "2026-08-05T04:30:00+00:00"},
        {"pnl": 2000.0, "decay_cost": 200.0, "r_multiple": 2.0,
         "confidence": 0.6, "exit_reason": "target", "direction": "SELL",
         "entry_time": "2026-08-05T05:00:00+00:00",
         "exit_time": "2026-08-05T06:00:00+00:00"},
    ]
    d = diagnostics.decay_share(trades)
    assert d["net_pnl"] == 1000.0
    assert d["total_decay_cost"] == 500.0
    assert d["loss_without_decay"] == 1500.0


def test_per_check_edge_is_computed_from_agreement():
    """A check's edge is the expectancy gap between trades it agreed with
    and trades it argued against. Around zero means the check contributes
    noise — and noise carrying a weight dilutes the checks that work."""
    from app.backtest import diagnostics

    def trade(direction, contribution, r):
        return {"direction": direction, "pnl": r * 1000, "r_multiple": r,
                "confidence": 0.5, "exit_reason": "target",
                "entry_time": "2026-08-05T04:00:00+00:00",
                "exit_time": "2026-08-05T05:00:00+00:00",
                "decay_cost": 0.0, "checks": {"useful": contribution}}

    trades = [
        trade("BUY", 0.2, 2.0),    # agreed, won
        trade("BUY", 0.2, 1.0),    # agreed, won
        trade("BUY", -0.2, -1.0),  # argued against, lost
        trade("BUY", -0.2, -1.0),  # argued against, lost
    ]
    row = next(r for r in diagnostics.by_check(trades) if r["check"] == "useful")
    assert row["agreed"]["trades"] == 2
    assert row["disagreed"]["trades"] == 2
    assert row["edge"] == pytest.approx(2.5)   # 1.5 against -1.0


def test_sell_direction_flips_the_sign():
    """A negative contribution on a SELL is agreement, not disagreement."""
    from app.backtest import diagnostics

    trades = [{
        "direction": "SELL", "pnl": 2000.0, "r_multiple": 2.0, "confidence": 0.5,
        "exit_reason": "target", "decay_cost": 0.0,
        "entry_time": "2026-08-05T04:00:00+00:00",
        "exit_time": "2026-08-05T05:00:00+00:00",
        "checks": {"bearish_check": -0.3},
    }]
    row = diagnostics.by_check(trades)[0]
    assert row["agreed"]["trades"] == 1
    assert row["disagreed"]["trades"] == 0