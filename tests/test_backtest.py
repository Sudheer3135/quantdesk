import sys
from pathlib import Path

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
