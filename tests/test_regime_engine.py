"""Classifying what kind of market a bar happened in.

Step 1 of the decision-desk work. The outcome study showed the signals lose
0.66R averaged over every condition the market can be in — an average of
things that should not be added together. This classifier is what lets the
average be split, so the two properties that matter most here are that it
tells the conditions apart at all, and that it cannot see the future.

The look-ahead test is the load-bearing one. A regime that peeked ahead
would make every split built on it meaningless in a way that looks exactly
like a discovery, so `test_classifying_a_prefix_gives_identical_verdicts`
asserts the strongest available form of the property: classifying the first
N bars produces bit-identical verdicts to classifying the whole archive and
reading bar N.
"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import indicators, regime
from app.market_hours import IST

BARS_PER_SESSION = 75          # 09:15 to 15:30 on a five-minute chart


def session_stamps(n, first_day=(2026, 6, 1)):
    """`n` five-minute stamps, rolling onto the next weekday at 09:15 IST."""
    day = datetime(*first_day, 9, 15, tzinfo=IST)
    out = []
    for i in range(n):
        out.append((day + timedelta(minutes=5 * (i % BARS_PER_SESSION)))
                   .astimezone(UTC))
        if (i + 1) % BARS_PER_SESSION == 0:
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
    return out


def frame_from_steps(steps, start=24_000.0, wick=2.0, volume=1000.0,
                     provenance=indicators.UNKNOWN):
    """A candle frame whose closes walk by `steps`.

    Volume provenance defaults to UNKNOWN, which is what a frame assembled
    out of nowhere honestly is: nobody vouched for these numbers, so
    volume-weighted features are unavailable. A test that needs them says
    so with `provenance=indicators.GENUINE`.
    """
    steps = np.asarray(steps, dtype=float)
    close = start + np.cumsum(steps)
    frame = pd.DataFrame({
        "timestamp": session_stamps(len(steps)),
        "open": close - steps,
        "high": np.maximum(close, close - steps) + wick,
        "low": np.minimum(close, close - steps) - wick,
        "close": close,
        "volume": [volume] * len(steps),
    })
    return indicators.declare_volume(frame, provenance)


def with_traded_volume(df, seed=5):
    """Declare this frame's volume genuine, as an exchange feed would.

    The values are varied so relative volume has something to measure, but
    it is the declaration that makes them usable — randomising the numbers
    is not a way to get past the provenance rule, and must not become one.
    """
    out = df.copy()
    out["volume"] = np.random.default_rng(seed).uniform(500, 5000, len(out))
    return indicators.declare_volume(out, indicators.GENUINE)


def calm(n, seed=0, scale=6.0):
    """Ordinary two-sided noise — the baseline the others are measured against."""
    return np.random.default_rng(seed).normal(0.0, scale, n)


def last_verdicts(df):
    out = regime.classify_frame(df)
    return out["day"].iloc[-1], out["hour"].iloc[-1]


# ---- the labels are actually distinguishable ---------------------------
#
# Every one of these appends its regime to the SAME calm baseline. That is
# deliberate: ATR is compared against its own recent average, so a frame that
# is chop from end to end has a chop baseline and reads as perfectly normal.
# The classifier answers "unusual for this market lately", and only a change
# within one series tests that.

def test_a_clean_one_way_move_is_a_trend_up():
    df = frame_from_steps(np.concatenate([calm(300), np.full(60, 4.0)]))
    day, hour = last_verdicts(df)

    assert day.label == regime.TREND_UP
    assert hour.label == regime.TREND_UP
    assert day.confidence > 0.5


def test_a_clean_one_way_move_down_is_a_trend_down():
    df = frame_from_steps(np.concatenate([calm(300), np.full(60, -4.0)]))
    day, _ = last_verdicts(df)

    assert day.label == regime.TREND_DOWN


def test_wide_bars_going_nowhere_are_volatile_chop():
    rng = np.random.default_rng(11)
    df = frame_from_steps(np.concatenate([calm(300), rng.normal(0, 30.0, 60)]))
    day, _ = last_verdicts(df)

    assert day.label == regime.VOLATILE_CHOP


def test_a_volatility_collapse_is_a_squeeze():
    """Needs declared traded volume: a squeeze is read partly off the VWAP
    bands, which are unavailable while the frame's volume is unvouched."""
    rng = np.random.default_rng(12)
    df = frame_from_steps(np.concatenate([calm(300), rng.normal(0, 0.8, 60)]))
    df = with_traded_volume(df)
    day, _ = last_verdicts(df)

    assert day.label == regime.SQUEEZE


def test_ordinary_two_sided_drift_is_a_range():
    df = frame_from_steps(calm(360, seed=5))
    day, _ = last_verdicts(df)

    assert day.label == regime.RANGE


def test_a_fast_clean_trend_is_not_called_chop():
    """Expansion alone must not mean chop.

    The naive rule — wide bars means volatile means chop — misreads exactly
    the market you most want to be in. Efficiency is what separates them.
    """
    rng = np.random.default_rng(13)
    burst = rng.normal(22.0, 8.0, 60)          # big bars, all one way
    df = frame_from_steps(np.concatenate([calm(300), burst]))
    day, _ = last_verdicts(df)

    assert day.label == regime.TREND_UP


def test_a_quiet_steady_drift_stays_a_trend_rather_than_a_squeeze():
    """Compression discounts a trend; it does not veto one."""
    df = frame_from_steps(np.concatenate([calm(300), np.full(60, 1.2)]))
    day, _ = last_verdicts(df)

    assert day.label == regime.TREND_UP


# ---- look-ahead --------------------------------------------------------

@pytest.mark.parametrize("cut", [120, 190, 260, 330, 400])
def test_classifying_a_prefix_gives_identical_verdicts(cut):
    """The property the whole regime split rests on.

    Not "roughly similar" — identical. Every feature is an expanding sum
    inside the session, a trailing window, or a Wilder average, all of which
    look backwards only. If a centred window or a whole-sample mean ever
    creeps in, this fails immediately, whereas a tolerance-based assertion
    would let a small leak through and the leak would show up later as a
    regime split that looked like a finding.
    """
    df = frame_from_steps(np.concatenate([calm(200, seed=3),
                                          np.full(100, 3.0),
                                          calm(120, seed=4)]))
    full = regime.classify_frame(df)
    prefix = regime.classify_frame(df.iloc[:cut].reset_index(drop=True))

    for level in ("day", "hour"):
        whole, partial = full[level].iloc[cut - 1], prefix[level].iloc[cut - 1]
        assert whole.label == partial.label
        assert whole.confidence == partial.confidence
        assert whole.scores == partial.scores


def test_a_later_shock_cannot_change_an_earlier_verdict():
    """The same property stated the way it would actually be violated."""
    base = calm(300, seed=8)
    quiet = frame_from_steps(np.concatenate([base, np.full(60, 0.5)]))
    shocked = frame_from_steps(np.concatenate([base, np.full(60, 0.5),
                                               np.full(60, 90.0)]))

    quiet_out = regime.classify_frame(quiet)
    shocked_out = regime.classify_frame(shocked)

    assert (quiet_out["day"].iloc[-1].label
            == shocked_out["day"].iloc[len(quiet) - 1].label)
    assert (quiet_out["day"].iloc[-1].confidence
            == shocked_out["day"].iloc[len(quiet) - 1].confidence)


# ---- transparency ------------------------------------------------------

def test_every_verdict_carries_reasons():
    """No black boxes. A label with no justification is one."""
    df = frame_from_steps(calm(200, seed=2))
    for _, row in regime.classify_frame(df).tail(30).iterrows():
        for level in ("day", "hour"):
            assert row[level].reasons
            assert all(isinstance(r, str) and r for r in row[level].reasons)


def test_the_reasons_name_the_numbers_that_drove_the_label():
    df = frame_from_steps(np.concatenate([calm(300), np.full(60, 4.0)]))
    df = with_traded_volume(df)      # so the VWAP reason can appear
    day, _ = last_verdicts(df)
    text = " ".join(day.reasons)

    assert "ATR" in text
    assert "Efficiency" in text
    assert "VWAP" in text


def test_every_label_is_scored_not_just_the_winner():
    """A near-tie has to be visible as a near-tie."""
    df = frame_from_steps(calm(200, seed=6))
    day, _ = last_verdicts(df)

    assert set(day.scores) == set(regime.LABELS)
    assert day.label == max(day.scores, key=day.scores.get)


def test_confidence_stays_inside_zero_and_one():
    df = frame_from_steps(np.concatenate([calm(200), np.full(80, 12.0),
                                          calm(80, seed=9)]))
    for _, row in regime.classify_frame(df).iterrows():
        for level in ("day", "hour"):
            assert 0.0 <= row[level].confidence <= 1.0


def test_a_near_tie_scores_lower_confidence_than_a_clear_read():
    """Separation from the runner-up is part of the confidence, so a market
    sitting between two conditions cannot report the same certainty as one
    that is plainly in one of them."""
    clear = regime.classify(regime.Features(
        level=regime.DAY, bars=60, efficiency=0.95, displacement_atr=4.0,
        atr_ratio=1.0, vwap_side=0.98, vwap_crossings=0.0))
    muddy = regime.classify(regime.Features(
        level=regime.DAY, bars=60, efficiency=0.45, displacement_atr=0.5,
        atr_ratio=0.72, vwap_side=0.6, vwap_crossings=0.2))

    assert clear.confidence > muddy.confidence


# ---- honest about what it does not know --------------------------------

def test_the_first_bars_of_a_session_are_marked_provisional():
    """The day view is allowed to speak early — that is the point of a
    session-expanding read — but half an hour is not a day, and the
    confidence is scaled to say so."""
    df = frame_from_steps(calm(BARS_PER_SESSION * 2 + 2, seed=7))
    out = regime.classify_frame(df)
    opening = out["day"].iloc[-1]        # second bar of a fresh session

    assert opening.provisional is True
    assert any("provisional" in r for r in opening.reasons)


def test_a_mature_session_is_not_provisional():
    df = frame_from_steps(calm(BARS_PER_SESSION + 40, seed=7))
    day, _ = last_verdicts(df)

    assert day.provisional is False


def test_synthetic_volume_is_reported_as_unavailable_not_as_neutral():
    """The free source reports a constant for NIFTY volume. Treating that as
    a neutral reading would let a placeholder vote."""
    df = frame_from_steps(calm(200), volume=1000.0)      # constant
    day, _ = last_verdicts(df)

    assert day.features["rvol"] is None
    assert any("Volume is unavailable or synthetic" in r for r in day.reasons)


def test_real_volume_is_used_and_named():
    df = with_traded_volume(frame_from_steps(calm(200)), seed=4)
    day, _ = last_verdicts(df)

    assert day.features["rvol"] is not None
    assert any("Relative volume" in r for r in day.reasons)


def test_an_archive_too_short_for_an_atr_baseline_says_so():
    df = frame_from_steps(calm(8))
    day, _ = last_verdicts(df)

    assert day.features["atr_ratio"] is None
    assert any("no baseline yet" in r for r in day.reasons)


# ---- shape and serialisation -------------------------------------------

def test_features_never_carry_nan_into_storage():
    """NaN is not valid JSON and Postgres would take it as a float. Every
    unmeasurable feature has to be None instead."""
    df = frame_from_steps(calm(BARS_PER_SESSION + 5, seed=1))
    for _, row in regime.classify_frame(df).iterrows():
        for level in ("day", "hour"):
            blob = json.dumps(row[level].to_dict())      # raises on NaN? no —
            assert "NaN" not in blob
            for value in row[level].features.values():
                if isinstance(value, float):
                    assert np.isfinite(value)


def test_classify_frame_covers_every_bar():
    df = frame_from_steps(calm(140, seed=10))
    out = regime.classify_frame(df)

    assert len(out) == len(df)
    assert list(out["timestamp"]) == list(pd.to_datetime(df["timestamp"], utc=True))


def test_labels_are_always_from_the_agreed_set():
    df = frame_from_steps(np.concatenate([calm(150), np.full(60, 9.0),
                                          calm(60, seed=2)]))
    for _, row in regime.classify_frame(df).iterrows():
        assert row["day"].label in regime.LABELS
        assert row["hour"].label in regime.LABELS


def test_an_empty_frame_classifies_to_nothing_rather_than_raising():
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                  "close", "volume"])
    assert regime.classify_frame(empty).empty
    assert regime.classify_latest(empty) is None


def test_classify_latest_is_the_last_bar_of_classify_frame():
    df = frame_from_steps(calm(180, seed=15))
    latest = regime.classify_latest(df)
    final = regime.classify_frame(df).iloc[-1]

    assert latest["day"]["label"] == final["day"].label
    assert latest["hour"]["label"] == final["hour"].label
    assert latest["engine_version"] == regime.ENGINE_VERSION


def test_no_evidence_is_reported_as_no_evidence():
    """A featureless read must not be dressed up as a confident RANGE."""
    verdict = regime.classify(regime.Features(level=regime.DAY, bars=60))

    assert verdict.label == regime.RANGE
    assert verdict.confidence == 0.0
    assert any("absence of evidence" in r for r in verdict.reasons)


# ---- the ramp ----------------------------------------------------------

def test_the_ramp_is_flat_outside_its_bounds_and_linear_inside():
    assert regime._ramp(0.1, 0.3, 0.6) == 0.0
    assert regime._ramp(0.9, 0.3, 0.6) == 1.0
    assert regime._ramp(0.45, 0.3, 0.6) == pytest.approx(0.5)
    assert regime._ramp_down(0.5, 0.85, 0.60) == pytest.approx(1.0)
    assert regime._ramp_down(0.9, 0.85, 0.60) == pytest.approx(0.0)


def test_thresholds_use_a_ramp_so_labels_do_not_flicker_on_a_hair():
    """Two markets a thousandth apart in efficiency must not be different
    kinds of market."""
    base = dict(level=regime.DAY, bars=60, atr_ratio=1.0,
                displacement_atr=1.0, vwap_side=0.7, vwap_crossings=0.05)
    a = regime.classify(regime.Features(efficiency=0.4499, **base))
    b = regime.classify(regime.Features(efficiency=0.4501, **base))

    assert a.label == b.label
    assert abs(a.confidence - b.confidence) < 0.01


# ---- the forming bar, at the regime entry point itself (2A.1) ----------

def forming_frame(n=320):
    """A frame whose last row is the bar currently being built."""
    df = frame_from_steps(np.concatenate([calm(n - 20, seed=3), np.full(20, 3.0)]))
    return df


def test_classify_latest_ignores_the_bar_still_forming():
    """The public entry point must hold back the forming bar itself.

    `plan.build` filtering first protected the plan, not this: anything
    calling the classifier directly — the dashboard, a notebook, a future
    caller — was handed a verdict computed on a bar that had not happened,
    stamped with that bar's timestamp.
    """
    df = forming_frame()
    closed_at = pd.Timestamp(df["timestamp"].iloc[-1])          # last closed bar opens here
    forming = df.iloc[[-1]].copy()
    forming["timestamp"] = closed_at + pd.Timedelta(minutes=5)
    forming[["open", "high", "low", "close"]] = [30_000., 31_000., 29_000., 30_500.]
    live = pd.concat([df, forming], ignore_index=True)
    live.attrs.update(df.attrs)
    decision = closed_at + pd.Timedelta(minutes=7, seconds=40)

    with_forming = regime.classify_latest(live, as_of=decision)
    without = regime.classify_latest(df, as_of=decision)

    assert with_forming == without                   # timestamp, labels, scores, everything
    assert pd.Timestamp(with_forming["timestamp"]) == closed_at


def test_classify_latest_sees_a_bar_the_instant_it_closes():
    """Zero finality delay, unchanged: closed means closed."""
    df = forming_frame()
    last_open = pd.Timestamp(df["timestamp"].iloc[-1])
    closes_at = last_open + pd.Timedelta(minutes=5)

    just_before = regime.classify_latest(df, as_of=closes_at - pd.Timedelta(milliseconds=1))
    exactly_at = regime.classify_latest(df, as_of=closes_at)

    assert pd.Timestamp(just_before["timestamp"]) == last_open - pd.Timedelta(minutes=5)
    assert pd.Timestamp(exactly_at["timestamp"]) == last_open


def test_classify_latest_reads_the_decision_clock_off_the_frame():
    """A frame carrying `decision_time` needs no argument at the call site."""
    df = forming_frame()
    last_open = pd.Timestamp(df["timestamp"].iloc[-1])
    df.attrs["decision_time"] = (last_open + pd.Timedelta(minutes=2)).isoformat()

    verdict = regime.classify_latest(df)
    assert pd.Timestamp(verdict["timestamp"]) == last_open - pd.Timedelta(minutes=5)


def test_classify_latest_says_nothing_when_no_bar_has_closed():
    df = forming_frame(n=30)
    before_any_close = pd.Timestamp(df["timestamp"].iloc[0]) - pd.Timedelta(minutes=1)
    assert regime.classify_latest(df, as_of=before_any_close) is None


# ---- participation cannot be carried forward (2A.3) --------------------

def test_an_unavailable_volume_bar_gets_no_participation_reading():
    """Earlier good readings must not vote for a bar that has none.

    The day-level relative volume is an expanding mean, and an expanding
    mean skips what it cannot average: after two valid bars a third with
    no volume observation inherited their average and reported ordinary
    participation — 1.0x — for a bar the desk could not see at all.
    """
    df = with_traded_volume(frame_from_steps(calm(200, seed=3)))
    df.loc[df.index[-1], "volume"] = np.nan          # the current bar is blind

    frame = regime._feature_frame(df)
    assert np.isnan(frame["rvol"].iloc[-1])
    assert np.isnan(frame["rvol_day"].iloc[-1])
    assert np.isnan(frame["rvol_hour"].iloc[-1])

    # History is intact; only the blind bar is silent. The reading on the
    # previous bar is exactly what it was before the blind bar existed.
    without = regime._feature_frame(df.iloc[:-1].reset_index(drop=True))
    assert np.isfinite(frame["rvol_day"].iloc[-2])
    assert frame["rvol_day"].iloc[-2] == pytest.approx(without["rvol_day"].iloc[-1])

    day, hour = last_verdicts(df)
    assert day.features["rvol"] is None
    assert hour.features["rvol"] is None
    assert any("Volume is unavailable or synthetic" in r for r in day.reasons)
    assert not any("Relative volume" in r for r in day.reasons)


def test_an_untrusted_folded_bar_gets_no_participation_reading():
    """The same rule reached through aggregation rather than a raw NaN."""
    from app.analytics import timeframes

    five = with_traded_volume(frame_from_steps(calm(600, seed=4)))
    flags = np.zeros(len(five), dtype=bool)
    flags[-2] = True                                  # one bad constituent
    five["volume_is_synthetic"] = flags
    folded = timeframes.fold(five, 3, as_of=pd.Timestamp(five["timestamp"].iloc[-1])
                             + pd.Timedelta(hours=2))

    frame = regime._feature_frame(folded)
    assert np.isnan(folded["volume"].iloc[-1])
    assert np.isnan(frame["rvol"].iloc[-1])
    assert np.isnan(frame["rvol_day"].iloc[-1])
