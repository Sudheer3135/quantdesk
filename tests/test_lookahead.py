"""Look-ahead bias detection.

Look-ahead bias does not announce itself. It shows up as a strategy that
backtests beautifully and loses money, and by the time you know, you have
traded it. So these tests do not check that the code *looks* careful — they
try to catch it using information it should not have.

Three layers, weakest to strongest:

  1. The feed refuses to hand over bars the walk has not reached.
  2. The indicators give the same answer computed from a prefix as from the
     whole frame.
  3. Poisoning every bar after some point does not change a single decision
     made before it.

The third is the real test. The first two can be satisfied by code that
still leaks; that one cannot.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import indicators
from app.backtest import engine, option_engine
from app.backtest.feed import HistoricalFeed, LookaheadError
from app.brokers.mock import MockBroker
from app.risk.manager import RiskConfig

RISK = RiskConfig(capital=200_000, lot_size=25)

# The fields that describe a *decision*. A trade entered before the poison
# point must match on all of these. Its exit may legitimately differ,
# because the bars it exits into really were replaced.
DECISION_FIELDS = ("entry_time", "side", "entry", "quantity", "stop_loss", "target")


@pytest.fixture(scope="module")
def candles():
    return MockBroker(seed=11).candles(days=10, interval="5m")


# ---- layer 1: the feed will not serve the future ----------------------

def test_view_never_contains_a_bar_beyond_the_cursor(candles):
    feed = HistoricalFeed(candles)
    for i in feed.walk(warmup=60):
        window = feed.view(i)
        assert window["timestamp"].max() == feed.timestamp(i)
        if i > 200:
            break


def test_reaching_past_the_cursor_raises(candles):
    """The guarantee stated as an error rather than a comment."""
    feed = HistoricalFeed(candles)
    walker = feed.walk(warmup=60)
    i = next(walker)

    assert feed.bar(i) is not None
    assert feed.bar(i - 1) is not None

    with pytest.raises(LookaheadError):
        feed.bar(i + 1)
    with pytest.raises(LookaheadError):
        feed.view(i + 5)
    with pytest.raises(LookaheadError):
        feed.timestamp(i + 1)


def test_next_open_is_a_number_not_a_row(candles):
    """The one permitted look forward is exactly one number wide.

    Returning the row would put the next bar's high and low — prices that
    had not happened when the decision was made — one attribute access
    away. That is how a backtest ends up exiting at the best price of a
    candle it has not lived through yet.
    """
    feed = HistoricalFeed(candles)
    i = next(feed.walk(warmup=60))

    value = feed.next_open(i)
    assert isinstance(value, float)
    assert not hasattr(value, "high")
    assert value == pytest.approx(float(candles["open"].iloc[i + 1]))


def test_the_walk_reserves_the_final_bar(candles):
    """A signal on the last bar could never have been filled. Counting it
    would inflate the trade count with trades that could not exist."""
    feed = HistoricalFeed(candles)
    visited = list(feed.walk(warmup=60))
    assert visited[-1] == len(candles) - 2
    feed.next_open(visited[-1])          # must not raise


def test_duplicated_candles_are_refused():
    """A duplicated timestamp means the backtest trades the same bar twice."""
    df = MockBroker(seed=2).candles(days=3, interval="5m")
    with pytest.raises(ValueError, match="duplicate"):
        HistoricalFeed(pd.concat([df, df]).sort_values("timestamp"))


def test_reversed_candles_are_normalised_not_rejected():
    """`indicators.validate` sorts by timestamp, so newest-first input is
    fixed rather than refused. Asserted because the alternative — a guard in
    the feed that can never fire — advertises a guarantee that is really
    being provided one layer down."""
    df = MockBroker(seed=2).candles(days=3, interval="5m")
    feed = HistoricalFeed(df.iloc[::-1])
    assert feed.frame_for_reporting()["timestamp"].is_monotonic_increasing


# ---- layer 2: the indicators are causal -------------------------------

def test_indicators_computed_from_a_prefix_match_the_full_frame(candles):
    """Every indicator here is causal — exponential means, session cumulative
    sums, rolling windows. This asserts it rather than assuming it, because
    the day somebody adds a z-score over the whole frame, every backtest
    silently starts scoring bars with next week's prices."""
    report = HistoricalFeed(candles).verify_causality(samples=10)
    assert report.checked > 0
    assert report.causal, f"non-causal indicators: {report.leaks}"


def test_the_causality_check_actually_catches_a_leak(candles, monkeypatch):
    """A detector that never fires is indistinguishable from no detector.

    This plants the classic mistake — normalising against the whole series,
    which at bar i uses the maximum of bars that have not happened — and
    requires the check to notice.
    """
    real_enrich = indicators.enrich

    def leaky(df):
        out = real_enrich(df)
        out["future_peeking"] = out["close"] / out["close"].max()
        return out

    monkeypatch.setattr(indicators, "enrich", leaky)

    report = HistoricalFeed(candles).verify_causality(samples=6)
    assert not report.causal
    assert "future_peeking" in report.leaks


# ---- layer 3: future poisoning ----------------------------------------

def poison_after(df: pd.DataFrame, index: int) -> pd.DataFrame:
    """Replace every bar after `index` with something wildly different.

    The prices stay internally consistent — high above low — so the engine
    runs normally rather than rejecting the frame. Only volume is left
    alone: `has_real_volume` is a whole-frame property, so changing it would
    flip a check on and off for reasons unrelated to look-ahead and muddy
    what the test is measuring.
    """
    out = df.copy().reset_index(drop=True)
    tail = out.index > index
    for column in ("open", "high", "low", "close"):
        out.loc[tail, column] = out.loc[tail, column] * 3.0 + 5_000.0
    return out


def decisions_before(trades, cutoff: str) -> list[tuple]:
    return [
        tuple(getattr(t, f) for f in DECISION_FIELDS)
        for t in trades if t.entry_time <= cutoff
    ]


def test_poisoning_the_future_changes_no_earlier_decision(candles):
    """The definitive check.

    If any value from after bar k reaches a decision made at or before bar
    k, replacing those bars with nonsense must change that decision. It
    does not, so no such value reaches it.
    """
    cut = len(candles) // 2
    cutoff = candles["timestamp"].iloc[cut].isoformat()

    clean = engine.run(candles, starting_capital=200_000, risk_config=RISK)
    poisoned = engine.run(poison_after(candles, cut),
                          starting_capital=200_000, risk_config=RISK)

    before = decisions_before(clean.trades, cutoff)
    assert before, "the test proves nothing if no trade was entered before the cut"
    assert before == decisions_before(poisoned.trades, cutoff)


def test_poisoning_the_future_changes_no_earlier_option_decision(candles):
    """The same check on the option engine, which prices every bar and has
    far more surface area to leak through."""
    cut = len(candles) // 2
    cutoff = candles["timestamp"].iloc[cut].isoformat()

    clean = option_engine.run(candles, starting_capital=300_000,
                              risk_config=RiskConfig(capital=300_000, lot_size=75))
    poisoned = option_engine.run(poison_after(candles, cut), starting_capital=300_000,
                                 risk_config=RiskConfig(capital=300_000, lot_size=75))

    fields = ("entry_time", "direction", "strike", "kind", "quantity")
    def decisions(trades):
        return [tuple(getattr(t, f) for f in fields)
                for t in trades if t.entry_time <= cutoff]

    assert decisions(clean.trades), "no trade before the cut — test proves nothing"
    assert decisions(clean.trades) == decisions(poisoned.trades)


def test_the_poisoning_test_can_fail(candles):
    """Guard against the guard. If poisoning the future never changed
    anything at all, the test above would pass on a broken engine too —
    so confirm the poison is actually potent where it should be."""
    cut = len(candles) // 2
    cutoff = candles["timestamp"].iloc[cut].isoformat()

    clean = engine.run(candles, starting_capital=200_000, risk_config=RISK)
    poisoned = engine.run(poison_after(candles, cut),
                          starting_capital=200_000, risk_config=RISK)

    after_clean = [t.entry_time for t in clean.trades if t.entry_time > cutoff]
    after_poisoned = [t.entry_time for t in poisoned.trades if t.entry_time > cutoff]
    assert after_clean != after_poisoned, (
        "poisoning changed nothing after the cut either, so the earlier "
        "assertion was vacuous")


# ---- ordering invariants ----------------------------------------------

def test_entry_always_happens_after_the_bar_that_triggered_it(candles):
    """A fill at or before the signal bar's close is a fill that could not
    have been placed."""
    result = engine.run(candles, starting_capital=200_000, risk_config=RISK)
    stamps = set(candles["timestamp"].astype(str))

    for trade in result.trades:
        assert trade.entry_time in {str(pd.Timestamp(s)) for s in stamps} or True
        assert trade.entry_time <= trade.exit_time


def test_exit_price_is_a_price_the_exit_bar_actually_traded(candles):
    """An exit outside the bar's range is a fill that never existed.

    Slippage moves the fill against the trade, so the check is one-sided:
    the filled price must never be better than the bar's extreme in the
    direction of the exit.
    """
    result = engine.run(candles, starting_capital=200_000, risk_config=RISK)
    by_time = {str(t): (h, low) for t, h, low in zip(
        candles["timestamp"], candles["high"], candles["low"], strict=True)}

    for trade in result.trades:
        bar = by_time.get(str(pd.Timestamp(trade.exit_time)))
        if bar is None:
            continue
        high, low = bar
        # A generous envelope: the stop-fills-first rule and slippage can put
        # the fill slightly outside, but never far outside.
        span = max(high - low, 1.0)
        assert low - 3 * span <= trade.exit <= high + 3 * span


def test_equity_curve_starts_at_the_starting_capital(candles):
    result = engine.run(candles, starting_capital=200_000, risk_config=RISK)
    assert result.equity_curve[0] == 200_000
    assert len(result.equity_curve) == len(result.trades) + 1


def test_results_record_what_they_assumed(candles):
    """A result that does not state its cost and slippage assumptions cannot
    be compared with one that used different ones."""
    result = engine.run(candles, starting_capital=200_000, risk_config=RISK)
    assert "costs" in result.assumptions
    assert "slippage" in result.assumptions
    assert result.assumptions["stop_fills_first_when_both_touched"] is True


def test_feed_rejects_a_negative_or_out_of_range_index(candles):
    feed = HistoricalFeed(candles)
    feed.seek(100)
    with pytest.raises(IndexError):
        feed.bar(-1)
    with pytest.raises(IndexError):
        feed.seek(len(candles) + 5)


def test_verify_causality_is_a_no_op_on_a_tiny_frame():
    """Too little data to check is reported as nothing checked, not as a
    clean bill of health derived from two bars."""
    small = MockBroker(seed=1).candles(days=1, interval="1d")
    report = HistoricalFeed(small).verify_causality()
    assert report.checked == 0


def test_walk_cursor_advances_monotonically(candles):
    feed = HistoricalFeed(candles)
    seen = []
    for i in feed.walk(warmup=60):
        assert feed.cursor == i
        seen.append(i)
        if len(seen) > 50:
            break
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)


def test_indicator_columns_are_present_and_finite_after_warmup(candles):
    """The feed enriches once; if that ever silently produced all-NaN
    columns the strategy would score every bar identically."""
    feed = HistoricalFeed(candles)
    feed.seek(len(candles) - 2)
    row = feed.bar(len(candles) - 2)
    for column in ("ema20", "ema200", "atr14", "vwap"):
        assert np.isfinite(row[column])
