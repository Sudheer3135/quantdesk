"""Splitting the evaluated signals by the condition they were formed in.

The point of the regime layer. The headline outcome study reports one
average across every condition the market can be in, which is an average of
things that should not be added together.

Two properties are load-bearing:

  The join is on the signal's own bar. Splitting outcomes by the regime
  visible after the trade resolved would be a look-ahead dressed up as an
  analysis — and it would look like a very good one, because the regime a
  winner ended in is partly caused by the winner.

  Unmatched signals are reported, not dropped. Silently discarding them
  would shrink the sample without saying so, and a split of 140 would be
  compared against a headline of 167 by a reader with no way to notice.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import regime_store, repository
from app.data.importer import import_index_candles
from app.evaluation import outcomes as study
from app.evaluation import regime_report
from app.market_hours import IST
from app.models import MarketRegime, SignalRecord

BARS_PER_SESSION = 75


def archive_frame(n=400, seed=2):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 6.0, n)
    close = 24_000 + np.cumsum(steps)
    day = datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    stamps = []
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
        "close": close, "volume": [1000.0] * n,
    })


def add_signal(db, frame, bar_index, action="BUY", confidence=0.6):
    """A signal computed on `bar_index`, stamped a moment after that bar
    closed — which is when the agent would actually have filed it.

    `timestamp` is the bar's *open*, so the close is one bar-width later.
    Stamping 30 seconds after the open instead described a decision taken
    while the bar was still forming, and the evaluator now correctly reads
    that as a decision made on the previous bar.
    """
    stamp = (pd.Timestamp(frame["timestamp"].iloc[bar_index])
             + timedelta(minutes=5)).to_pydatetime()
    price = float(frame["close"].iloc[bar_index])
    risk = 20.0 if action == "BUY" else -20.0
    record = SignalRecord(
        symbol="NIFTY", timeframe="5m", action=action, confidence=confidence,
        price=price, entry=price, stop_loss=price - risk,
        target=price + 2 * risk, checks=[], context={"trend": "bullish"},
        created_at=(stamp + timedelta(seconds=30)).astimezone(UTC))
    db.add(record)
    db.commit()
    return record


@pytest.fixture
def desk(db):
    """An archive, some in-session signals on it, and regimes classified."""
    frame = archive_frame()
    import_index_candles(db, frame, "NIFTY", "5m", source="test")
    for index in (100, 140, 180, 220, 260, 300):
        add_signal(db, frame, index)
    regime_store.backfill(db, "NIFTY", "5m")
    return frame


# ---- the split ---------------------------------------------------------

def test_every_evaluated_signal_lands_in_a_bucket(db, desk):
    report = regime_report.build(db, "NIFTY", "5m")
    selected = report.selection["selected"]

    assert selected > 0
    assert sum(b["n"] for b in report.by_day_regime) == selected
    assert sum(b["n"] for b in report.by_hour_regime) == selected


def test_the_split_totals_match_the_headline_study(db, desk):
    """The same outcomes, bucketed differently — never a second sample."""
    headline = study.evaluate(db, "NIFTY", "5m", include_outcomes=False)
    split = regime_report.build(db, "NIFTY", "5m")

    assert split.selection == headline.selection
    assert split.overall["n"] == headline.overall["n"]
    assert split.overall["win_rate"] == headline.overall["win_rate"]
    assert split.overall["avg_r"] == headline.overall["avg_r"]


def test_win_rate_is_computed_by_the_same_function_as_the_headline(db, desk):
    """Not merely equal by luck: the buckets come from `outcomes.group_by`,
    so a change to how a win is counted moves both together or neither."""
    split = regime_report.build(db, "NIFTY", "5m")
    rebuilt = study.group_by(
        study.collect(db, "NIFTY", "5m").outcomes, lambda r: "all")

    assert split.overall["win_rate"] == rebuilt[0]["win_rate"]
    assert split.overall["avg_r"] == rebuilt[0]["avg_r"]


def test_signals_are_matched_on_their_own_bar_not_the_exit_bar(db, desk):
    """The look-ahead this join could easily have contained.

    The regime of the bar the signal fired on was knowable at that moment;
    the regime at the exit was not, and it is partly *caused* by the trade
    working. Asserting the matched label equals the stored label for the
    signal bar pins the join to the causal side.
    """
    outcomes = study.collect(db, "NIFTY", "5m").outcomes
    assert outcomes

    stored = {pd.Timestamp(r.timestamp).isoformat(): r.day_regime
              for r in db.scalars(select(MarketRegime)).all()}
    frame = regime_store.load(db, "NIFTY", "5m")
    by_stamp = dict(zip(frame["timestamp"].map(lambda t: t.isoformat()),
                        frame["day_regime"], strict=True))

    for row in outcomes:
        signal_bar = pd.Timestamp(row.signal_bar_time).isoformat()
        exit_bar = pd.Timestamp(row.entry_time).isoformat()
        assert signal_bar in by_stamp
        # The bar the signal was computed on is strictly before the fill.
        assert signal_bar < exit_bar

    report = regime_report.build(db, "NIFTY", "5m")
    assert report.matched == len(outcomes)
    assert set(b["label"] for b in report.by_day_regime) <= set(stored.values())


def test_the_signal_bar_is_the_last_bar_that_had_closed(db, desk):
    """Both clocks around the decision, stated as the rule rather than as a
    fixed gap.

    The bar the signal was computed on is the last one that had *closed*
    when the row was filed, and the fill is the first bar that starts at or
    after that instant. Those are two different bars whenever the decision
    lands mid-bar, which is the normal case: a signal filed 30 seconds
    after a bar closes cannot be filled at the open of the bar already
    running, so the fill is the bar after that one. Asserting a gap of
    exactly one bar was asserting that no time passes between a bar closing
    and a decision being made.
    """
    frame = regime_store.load(db, "NIFTY", "5m")
    stamps = list(frame["timestamp"])
    width = pd.Timedelta(minutes=5)

    for row in study.collect(db, "NIFTY", "5m").outcomes:
        decision = pd.Timestamp(row.signal_time)
        signal_bar = pd.Timestamp(row.signal_bar_time)
        entry_bar = pd.Timestamp(row.entry_time)

        # The signal bar had closed; the bar after it had not.
        assert signal_bar + width <= decision
        assert stamps[stamps.index(signal_bar) + 1] + width > decision
        # The fill is the first bar that had not started yet.
        assert entry_bar >= decision
        assert stamps[stamps.index(entry_bar) - 1] < decision
        assert stamps.index(entry_bar) > stamps.index(signal_bar)


# ---- honest about gaps -------------------------------------------------

def test_unmatched_signals_are_reported_not_dropped(db):
    """A regime table behind the candle archive must be visible."""
    frame = archive_frame()
    import_index_candles(db, frame, "NIFTY", "5m", source="test")
    for index in (100, 200, 300):
        add_signal(db, frame, index)
    # No backfill at all.

    report = regime_report.build(db, "NIFTY", "5m")

    assert report.matched == 0
    assert report.unmatched == report.selection["selected"]
    assert [b["label"] for b in report.by_day_regime] == ["unclassified"]
    assert sum(b["n"] for b in report.by_day_regime) == report.selection["selected"]


def test_no_stored_regimes_points_at_the_backfill(db):
    frame = archive_frame()
    import_index_candles(db, frame, "NIFTY", "5m", source="test")
    add_signal(db, frame, 100)

    report = regime_report.build(db, "NIFTY", "5m")

    assert "backfill" in report.caveats[0]


def test_a_partially_classified_archive_splits_the_count(db, desk):
    """Half the bars classified must read as half matched, not as a smaller
    but complete-looking study."""
    frame = regime_store.load(db, "NIFTY", "5m")
    cutoff = frame["timestamp"].iloc[len(frame) // 2]
    for row in db.scalars(select(MarketRegime)).all():
        # `as_utc` for the usual reason: naive on SQLite, aware on Postgres,
        # and comparing either against the other raises.
        if pd.Timestamp(repository.as_utc(row.timestamp)) < cutoff:
            db.delete(row)
    db.commit()

    report = regime_report.build(db, "NIFTY", "5m")

    assert report.matched + report.unmatched == report.selection["selected"]
    if report.unmatched:
        assert any(b["label"] == "unclassified" for b in report.by_day_regime)


def test_a_mixed_version_table_is_flagged_before_the_numbers(db, desk):
    row = db.scalars(select(MarketRegime).limit(1)).first()
    row.engine_version = "0.9"
    db.commit()

    report = regime_report.build(db, "NIFTY", "5m")

    assert "more than one engine version" in report.caveats[0]
    assert report.regime_coverage["mixed_versions"] is True


# ---- reading the output ------------------------------------------------

def test_small_buckets_are_labelled_as_too_small_to_read(db, desk):
    """A 100% win rate on n=2 must not be presented as a result."""
    report = regime_report.build(db, "NIFTY", "5m")

    for bucket in report.by_day_regime:
        assert "interpretation" in bucket
        if 0 < (bucket["resolved"] or 0) < regime_report.MIN_MEANINGFUL:
            assert "too few" in bucket["interpretation"]


def test_the_report_says_the_split_is_not_a_new_study(db, desk):
    report = regime_report.build(db, "NIFTY", "5m")
    text = " ".join(report.caveats)

    assert "Split of an existing study" in text
    assert "causal" in text
    assert "gross" in text


def test_the_day_and_hour_agreement_split_covers_every_signal(db, desk):
    report = regime_report.build(db, "NIFTY", "5m")
    labels = {b["label"] for b in report.agreement}

    assert labels <= {"agree", "disagree"}
    assert sum(b["n"] for b in report.agreement) == report.selection["selected"]


def test_the_direction_split_is_nested_inside_the_regime_split(db, desk):
    report = regime_report.build(db, "NIFTY", "5m")

    assert sum(b["n"] for b in report.by_day_regime_and_direction) == \
        report.selection["selected"]
    for bucket in report.by_day_regime_and_direction:
        assert " / " in bucket["label"]


def test_an_empty_desk_produces_an_empty_report_rather_than_raising(db):
    report = regime_report.build(db, "NIFTY", "5m")

    assert report.overall["n"] == 0
    assert report.by_day_regime == []
    assert report.matched == 0


def test_the_report_is_json_serialisable(db, desk):
    import json
    report = regime_report.build(db, "NIFTY", "5m")

    assert json.loads(json.dumps(report.to_dict(), default=str))
