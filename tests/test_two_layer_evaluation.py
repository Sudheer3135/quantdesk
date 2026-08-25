"""Measuring bias and entry timing as separate questions.

The reason the output was split is that direction and timing fail
independently. Step 1 is the proof: eleven signals fired BUY into an hour
already trending up and averaged an MFE of 0.059R. As one number that reads
as a broken strategy; as two, it is a correct direction and an indefensible
entry, and only the second needs fixing.

Two properties here are worth more than the rest.

`test_bias_accuracy_is_reported_against_the_base_rate` — an accuracy figure
with no base rate beside it is unreadable. If 58% of hours in a sample closed
up, a permanently bullish model scores 58% and knows nothing.

`test_the_replay_is_not_written_back_into_the_signal_rows` — the plans are
recomputed, and a recomputed answer stored into a historical row would be a
fabricated audit record.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import plan as plan_builder
from app.data.importer import import_index_candles
from app.evaluation import two_layer
from app.market_hours import IST
from app.models import SignalRecord

BARS_PER_SESSION = 75


def archive(n=500, seed=3):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 6.0, n)
    close = 24_000 + np.cumsum(steps)
    stamps, day = [], datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    for i in range(n):
        stamps.append((day + timedelta(minutes=5 * (i % BARS_PER_SESSION)))
                      .astimezone(UTC))
        if (i + 1) % BARS_PER_SESSION == 0:
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
    return pd.DataFrame({
        "timestamp": stamps, "open": close - steps,
        "high": np.maximum(close, close - steps) + 12,
        "low": np.minimum(close, close - steps) - 12,
        "close": close, "volume": [1000.0] * n})


def add_signal(db, frame, index, action="BUY"):
    stamp = pd.Timestamp(frame["timestamp"].iloc[index]).to_pydatetime()
    price = float(frame["close"].iloc[index])
    risk = 20.0 if action == "BUY" else -20.0
    db.add(SignalRecord(
        symbol="NIFTY", timeframe="5m", action=action, confidence=0.6,
        price=price, entry=price, stop_loss=price - risk,
        target=price + 2 * risk, checks=[], context={"trend": "bullish"},
        created_at=(stamp + timedelta(seconds=30)).astimezone(UTC)))
    db.commit()


@pytest.fixture
def desk(db):
    frame = archive()
    import_index_candles(db, frame, "NIFTY", "5m", source="test")
    for index in (200, 240, 280, 320, 360, 400):
        add_signal(db, frame, index)
    return frame


# ---- the replay --------------------------------------------------------

def test_every_evaluated_signal_gets_a_plan(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")

    assert report.replayed == report.selection["selected"]
    assert report.replayed > 0
    assert report.plan_failures == 0


def test_the_replay_uses_only_bars_up_to_the_signal(db, desk):
    """The property that makes the reinterpretation legitimate rather than
    hindsight. Truncating the archive to the signal's own bar must not change
    the plan that signal gets."""
    from app.data import repository

    report = two_layer.build(db, "NIFTY", "5m", include_rows=True)
    candles = repository.load_index_candles(db, "NIFTY", "5m")
    stamps = list(pd.to_datetime(candles["timestamp"], utc=True))

    for row in report.rows[:3]:
        index = stamps.index(pd.Timestamp(row["signal_bar_time"]))
        window = candles.iloc[
            max(0, index - two_layer.PLAN_LOOKBACK_BARS + 1) : index + 1]
        rebuilt = plan_builder.build(window)

        assert rebuilt.bias["label"] == row["bias"]
        assert rebuilt.entry["state"] == row["entry_state"]


def test_the_replay_is_not_written_back_into_the_signal_rows(db, desk):
    """A recomputed answer stored into a historical row is a fabricated audit
    record. The study holds its plans in memory and says so."""
    two_layer.build(db, "NIFTY", "5m")

    for record in db.scalars(select(SignalRecord)).all():
        assert record.bias is None
        assert record.entry_state is None
        assert record.plan is None


def test_the_study_says_it_is_a_study(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")
    text = " ".join(report.caveats)

    assert "recomputed" in text
    assert "nothing here is stored back" in text
    assert "not trade success" in text


# ---- bias accuracy -----------------------------------------------------

def test_bias_accuracy_is_reported_against_the_base_rate(db, desk):
    """Accuracy alone is unreadable. If most hours closed up, a permanently
    bullish model scores well and knows nothing."""
    found = two_layer.build(db, "NIFTY", "5m").bias_accuracy

    assert "accuracy" in found
    assert "base_rate" in found
    assert "edge_over_base_rate" in found
    if found["accuracy"] is not None:
        assert found["edge_over_base_rate"] == pytest.approx(
            found["accuracy"] - found["base_rate"], abs=1e-9)


def test_a_neutral_bias_makes_no_claim_and_is_scored_as_none():
    """Counting NEUTRAL either way would let a model that never commits move
    its own score by staying quiet."""
    assert two_layer._bias_verdict(plan_builder.NEUTRAL, 3.0) == two_layer.NO_CLAIM
    assert two_layer._bias_verdict(plan_builder.NEUTRAL, -3.0) == two_layer.NO_CLAIM


def test_a_move_too_small_to_matter_is_flat_not_correct():
    """Without this a bias is scored right for a two-point drift and the
    accuracy figure becomes a coin flip wearing a measurement's clothes."""
    tiny = two_layer.BIAS_FLAT_ATR / 2
    assert two_layer._bias_verdict(plan_builder.BULLISH, tiny) == two_layer.FLAT
    assert two_layer._bias_verdict(plan_builder.BEARISH, -tiny) == two_layer.FLAT


def test_a_bias_is_correct_when_price_goes_its_way():
    big = two_layer.BIAS_FLAT_ATR * 4
    assert two_layer._bias_verdict(plan_builder.BULLISH, big) == two_layer.CORRECT
    assert two_layer._bias_verdict(plan_builder.BULLISH, -big) == two_layer.WRONG
    assert two_layer._bias_verdict(plan_builder.BEARISH, -big) == two_layer.CORRECT
    assert two_layer._bias_verdict(plan_builder.BEARISH, big) == two_layer.WRONG


def test_a_bias_with_no_forward_bars_left_makes_no_claim():
    """A signal too near the end of the archive has nothing to be judged
    against — not a wrong call, an unjudgeable one."""
    assert two_layer._bias_verdict(plan_builder.BULLISH, None) == two_layer.NO_CLAIM


def test_the_accuracy_counts_add_up(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")
    counts = report.bias_accuracy["counts"]

    assert sum(counts.values()) == report.replayed
    assert report.bias_accuracy["judged"] == counts["correct"] + counts["wrong"]


# ---- entry timing ------------------------------------------------------

def test_entry_states_cover_every_replayed_signal(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")

    assert sum(b["n"] for b in report.entry_states) == report.replayed
    assert {b["label"] for b in report.entry_states} <= set(plan_builder.ENTRY_STATES)


def test_every_bucket_says_whether_its_sample_is_readable(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")

    for bucket in report.entry_states:
        assert "interpretation" in bucket
        if bucket["n"] < two_layer.MIN_MEANINGFUL:
            assert "too few" in bucket["interpretation"]


def test_timing_is_measured_separately_from_direction(db, desk):
    """The two questions must be answerable one without the other. A bucket
    reports bias accuracy and trade behaviour side by side, never fused."""
    report = two_layer.build(db, "NIFTY", "5m")

    for bucket in report.entry_states:
        assert "bias_accuracy" in bucket
        assert "avg_mfe_r" in bucket
        assert "favour_first_rate" in bucket


def test_favour_first_is_true_when_the_trade_runs_immediately(db):
    """The timing measure itself: did the trade reach half its risk in favour
    before half against?"""
    from app.backtest.feed import HistoricalFeed

    frame = archive(n=120, seed=9)
    # A clean run up from bar 60 onward, so a long entered there is never
    # offside.
    frame.loc[60:, "close"] = 24_000 + np.arange(len(frame) - 60) * 8.0
    frame.loc[60:, "high"] = frame.loc[60:, "close"] + 4
    frame.loc[60:, "low"] = frame.loc[60:, "close"] - 1

    feed = HistoricalFeed(frame)
    assert two_layer._favour_first(feed, 61, "BUY",
                                   float(frame["close"].iloc[60]), 20.0) is True


def test_a_bar_covering_both_thresholds_is_read_pessimistically(db):
    """Five-minute bars do not record which came first, matching the outcome
    study's stop-first convention."""
    from app.backtest.feed import HistoricalFeed

    frame = archive(n=120, seed=9)
    frame.loc[61, "high"] = float(frame["close"].iloc[60]) + 50
    frame.loc[61, "low"] = float(frame["close"].iloc[60]) - 50

    feed = HistoricalFeed(frame)
    assert two_layer._favour_first(feed, 61, "BUY",
                                   float(frame["close"].iloc[60]), 20.0) is False


# ---- the reinterpretation ----------------------------------------------

def test_the_reinterpretation_splits_allowed_from_held(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")
    split = report.reinterpretation

    assert (split["would_enter_now"]["n"]
            + split["would_wait_or_refuse"]["n"]) == report.replayed
    assert "did not gate" in split["note"]


def test_the_reinterpretation_refuses_to_call_itself_a_backtest(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")

    assert "not independent" in " ".join(report.caveats)
    assert "Suggestive at best" in report.reinterpretation["note"]


def test_agreement_with_the_existing_engine_is_reported(db, desk):
    report = two_layer.build(db, "NIFTY", "5m")
    found = report.engine_agreement

    assert found["agree"] + found["disagree"] == found["comparable"]
    assert found["comparable"] + found["neutral_bias"] == report.replayed


def test_an_empty_desk_reports_nothing_rather_than_raising(db):
    report = two_layer.build(db, "NIFTY", "5m")

    assert report.replayed == 0
    assert report.entry_states == []
    assert report.bias_accuracy == {}


def test_the_report_is_json_serialisable(db, desk):
    import json
    report = two_layer.build(db, "NIFTY", "5m", include_rows=True)

    assert json.loads(json.dumps(report.to_dict(), default=str))
