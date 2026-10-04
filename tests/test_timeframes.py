"""Folding 5-minute bars into 15-minute and hourly ones.

One property carries this whole file: **a bar that is still forming must not
be emitted**. At 10:20 the 15-minute bar covering 10:15–10:30 exists as two
of its three pieces, and its close has not happened. Reading it as finished
would put the move the desk is deciding about into the context it uses to
decide — the future arriving one bar early, wearing the clothes of history.

The second property is about NSE specifically. The session is 6h15m, which
does not divide into hours, so every day ends with a 15:15–15:30 stub. That
stub is a real closed hourly bar once the day is over and must not be
discarded forever; inside the current session it is still forming and must
be held back. Both directions are tested.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import indicators, timeframes
from app.market_hours import IST

BARS_PER_SESSION = 75          # 09:15 to 15:25, five minutes apart


def five_minute(sessions=2, bars=BARS_PER_SESSION, start=24_000.0, step=1.0,
                provenance=indicators.GENUINE):
    """A clean ramp, so every aggregate is arithmetic anyone can check.

    Declared genuine by default: it stands in for an exchange feed that
    reports traded volume, which is what makes the volume arithmetic below
    meaningful. Tests about unvouched or substituted volume pass their own
    provenance.
    """
    stamps, day = [], datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    for _ in range(sessions):
        for i in range(bars):
            stamps.append((day + timedelta(minutes=5 * i)).astimezone(UTC))
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    n = len(stamps)
    close = start + np.arange(n) * step
    frame = pd.DataFrame({
        "timestamp": stamps, "open": close - step,
        "high": close + 2, "low": close - 3,
        "close": close, "volume": [100.0] * n})
    return indicators.declare_volume(frame, provenance)


def ist_times(frame):
    return [t.tz_convert("Asia/Kolkata").strftime("%H:%M") for t in frame["timestamp"]]


# ---- the arithmetic ----------------------------------------------------

def test_a_fifteen_minute_bar_is_three_five_minute_bars(x=None):
    df = five_minute(sessions=1)
    folded = timeframes.fifteen_minute(df)
    first, source = folded.iloc[0], df.iloc[0:3]

    assert first["open"] == source["open"].iloc[0]
    assert first["close"] == source["close"].iloc[-1]
    assert first["high"] == source["high"].max()
    assert first["low"] == source["low"].min()
    assert first["volume"] == source["volume"].sum()


def test_the_timestamp_is_the_groups_first_bar(x=None):
    """Left-labelled, like every other bar in this codebase."""
    df = five_minute(sessions=1)
    folded = timeframes.fifteen_minute(df)

    assert folded["timestamp"].iloc[0] == df["timestamp"].iloc[0]
    assert folded["timestamp"].iloc[1] == df["timestamp"].iloc[3]


def test_bars_are_anchored_on_the_session_open_not_the_wall_clock():
    """NSE opens at 09:15. `pandas.resample` would anchor on midnight and put
    the first bar of every day into a 09:00–10:00 bucket that starts before
    the market does."""
    df = five_minute(sessions=1)

    assert ist_times(timeframes.fifteen_minute(df))[:3] == ["09:15", "09:30", "09:45"]
    assert ist_times(timeframes.hourly(df))[:3] == ["09:15", "10:15", "11:15"]


def test_a_full_session_folds_to_the_expected_counts():
    df = five_minute(sessions=1)

    # 75 bars divide exactly into 25 fifteen-minute bars.
    assert len(timeframes.fifteen_minute(df)) == 25
    # Six full hours, and the 15:15 stub withheld — this session is the last
    # one in the frame, so as far as the folder can tell it is still running.
    assert len(timeframes.hourly(df)) == 7


def test_groups_never_span_a_session_boundary():
    """Yesterday's close and today's open are not one bar."""
    df = five_minute(sessions=2)
    folded = timeframes.hourly(df)
    days = folded["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date

    for _, group in folded.groupby(days):
        assert ist_times(group)[0] == "09:15"


# ---- the forming bar ---------------------------------------------------

def test_an_incomplete_group_is_held_back():
    """The look-ahead this module exists to prevent."""
    df = five_minute(sessions=1, bars=5)      # 09:15..09:35: one full 15m + 2
    folded = timeframes.fifteen_minute(df)

    assert len(folded) == 1
    assert ist_times(folded) == ["09:15"]


@pytest.mark.parametrize("bars,expected", [(3, 1), (4, 1), (5, 1), (6, 2), (8, 2)])
def test_only_completed_fifteen_minute_groups_are_emitted(bars, expected):
    folded = timeframes.fifteen_minute(five_minute(sessions=1, bars=bars))
    assert len(folded) == expected


def test_the_forming_bar_cannot_change_a_bar_already_emitted():
    """Stated the way it would actually be violated: adding the rest of the
    forming group must not restate anything already published."""
    partial = five_minute(sessions=1, bars=7)
    complete = five_minute(sessions=1, bars=9)

    a = timeframes.fifteen_minute(partial)
    b = timeframes.fifteen_minute(complete)

    assert len(a) == 2 and len(b) == 3
    pd.testing.assert_frame_equal(a, b.iloc[:2].reset_index(drop=True))


def test_a_growing_frame_only_ever_appends():
    """Every prefix must agree with every longer one on their overlap."""
    df = five_minute(sessions=2)
    reference = timeframes.hourly(df)

    for cut in (30, 61, 75, 100, 140):
        prefix = timeframes.hourly(df.iloc[:cut].reset_index(drop=True))
        pd.testing.assert_frame_equal(
            prefix, reference.iloc[: len(prefix)].reset_index(drop=True))


# ---- the end-of-session stub -------------------------------------------

def test_a_finished_sessions_short_last_hour_is_kept():
    """15:15–15:30 is three bars, not twelve, and it is a real closed hourly
    bar once the day is over. Dropping it would silently lose the last
    fifteen minutes of every trading day, forever."""
    df = five_minute(sessions=2)
    folded = timeframes.hourly(df)

    first_day = folded[folded["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date
                       == pd.Timestamp("2026-06-01").date()]
    assert ist_times(first_day)[-1] == "15:15"
    assert len(first_day) == 7


def test_the_session_stub_is_available_at_its_actual_close():
    """The same three bars, while their day is the latest in the frame, are a
    group that could still receive a fourth — so they are withheld.

    The cost is real and deliberate: after the close, the hourly view is
    missing the day's last fifteen minutes until the next session begins.
    That is the conservative direction. Withholding a finished bar loses
    information for a few hours; publishing an unfinished one invents it, and
    the invention is invisible.
    """
    df = five_minute(sessions=1)
    folded = timeframes.hourly(df)

    assert ist_times(folded)[-1] == "15:15"
    assert folded["close"].iloc[-1] == df["close"].iloc[-1]

    # And once a later session exists, the same stub is published.
    later = timeframes.hourly(five_minute(sessions=2))
    first_day = later[later["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date
                      == pd.Timestamp("2026-06-01").date()]
    assert ist_times(first_day)[-1] == "15:15"


# ---- shape and edges ---------------------------------------------------

def test_the_result_is_an_ordinary_candle_frame():
    """So `enrich`, `analyse` and everything else work on it unchanged."""
    folded = timeframes.fifteen_minute(five_minute())

    assert list(folded.columns) == ["timestamp", "open", "high", "low",
                                    "close", "volume"]
    assert folded["timestamp"].is_monotonic_increasing
    assert str(folded["timestamp"].dt.tz) == "UTC"


def test_an_empty_frame_folds_to_an_empty_frame():
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                  "close", "volume"])
    folded = timeframes.fifteen_minute(empty)

    assert folded.empty
    assert list(folded.columns) == ["timestamp", "open", "high", "low",
                                    "close", "volume"]


def test_a_frame_shorter_than_one_group_folds_to_nothing():
    folded = timeframes.fifteen_minute(five_minute(sessions=1, bars=2))
    assert folded.empty


def test_synthetic_volume_stays_synthetic_after_folding():
    """A declared substitute must still be a declared substitute at 15m.

    Aggregation is where a provenance claim is easiest to lose: the sums
    are new numbers, and if the claim were re-derived from them the bias
    layer would start trusting a placeholder it could not see through.
    """
    df = indicators.declare_volume(five_minute(), indicators.SYNTHETIC)
    folded = timeframes.fifteen_minute(df)

    assert indicators.volume_provenance(folded) == indicators.SYNTHETIC
    assert not indicators.has_real_volume(df)
    assert not indicators.has_real_volume(folded)


def test_unvouched_volume_stays_unvouched_after_folding():
    """The separate case: nobody declared anything about these numbers."""
    df = five_minute(provenance=indicators.UNKNOWN)
    folded = timeframes.fifteen_minute(df)

    assert indicators.volume_provenance(folded) == indicators.UNKNOWN
    assert not indicators.has_real_volume(df)
    assert not indicators.has_real_volume(folded)


def test_genuine_volume_survives_folding():
    """And the claim is not lost in the other direction either."""
    df = indicators.declare_volume(five_minute(), indicators.GENUINE)
    folded = timeframes.fifteen_minute(df)

    assert indicators.volume_provenance(folded) == indicators.GENUINE
    assert indicators.has_real_volume(folded)
    # Three five-minute bars of 100 fold to one fifteen-minute bar of 300.
    assert folded["volume"].iloc[0] == 300.0


def test_a_nonsense_group_size_is_refused():
    with pytest.raises(ValueError):
        timeframes.fold(five_minute(), 0)


# ---- volume validity through aggregation (2A.2) ------------------------
#
# A fifteen-minute bar is only as trustworthy as the five-minute bars
# inside it. `sum` alone says otherwise: it skips what it cannot add and
# returns a confident number, which is how [100, NaN, 300] became a
# plausible 400 and a flagged constituent disappeared into a bigger total.

LATE = pd.Timestamp("2026-06-01 16:00", tz="Asia/Kolkata")


def constituents(volumes, *, provenance=indicators.GENUINE, flags=None):
    """A short session of five-minute bars with the volume column stated."""
    n = len(volumes)
    stamps = pd.date_range("2026-06-01 09:15", periods=n, freq="5min",
                           tz="Asia/Kolkata").tz_convert(UTC)
    frame = pd.DataFrame({"timestamp": stamps, "open": 100.0, "high": 100.1,
                          "low": 99.9, "close": 100.0, "volume": volumes})
    if flags is not None:
        frame["volume_is_synthetic"] = flags
    return indicators.declare_volume(frame, provenance)


def folded_volume(frame):
    """(value, usable) for each higher-timeframe bar."""
    out = timeframes.fold(frame, 3, as_of=LATE)
    usable = indicators.volume_weights(out).notna()
    return list(zip(out["volume"].tolist(), [bool(u) for u in usable], strict=True))


def test_all_genuine_constituents_aggregate_to_a_trusted_total():
    assert folded_volume(constituents([100., 200., 300.])) == [(600.0, True)]


def test_a_genuine_zero_is_a_real_observation_not_a_gap():
    """No trading in one bar is information, and it sums like any number."""
    assert folded_volume(constituents([100., 0., 300.])) == [(400.0, True)]


def test_one_synthetic_constituent_makes_the_whole_bar_untrusted():
    """The number itself has to be unusable.

    Reporting 600 and marking it untrusted was not enough: the mark lives
    in a column that a six-column projection drops, and the 600 survived
    it on a frame still claiming genuine volume.
    """
    [(value, usable)] = folded_volume(constituents([100., 200., 300.],
                                                   flags=[False, True, False]))
    assert np.isnan(value)
    assert usable is False


@pytest.mark.parametrize("provenance", [indicators.UNKNOWN,
                                        indicators.UNAVAILABLE,
                                        indicators.SYNTHETIC])
def test_an_untrusted_frame_cannot_be_aggregated_into_a_trusted_one(provenance):
    [(_, usable)] = folded_volume(constituents([100., 200., 300.],
                                               provenance=provenance))
    assert usable is False


def test_a_missing_constituent_leaves_the_bar_with_no_total():
    """`sum` skipping the gap produced a total no exchange ever printed."""
    [(value, usable)] = folded_volume(constituents([100., np.nan, 300.]))
    assert np.isnan(value)
    assert usable is False


def test_a_later_bad_bar_does_not_taint_an_earlier_clean_one():
    out = folded_volume(constituents([100., 200., 300., 100., 200., 300.],
                                     flags=[False] * 4 + [True, False]))
    assert out[0] == (600.0, True)
    assert out[1][1] is False


@pytest.mark.parametrize("tail,flags", [
    ([50., 60., 70.], [False] * 6 + [True, False, False]),     # synthetic
    ([50., np.nan, 70.], None),                                # missing
    ([50., 60., 70.], None),                                   # clean
])
def test_completed_aggregates_do_not_change_when_later_bars_arrive(tail, flags):
    """Prefix invariance, including validity and not only the numbers."""
    clean = [100., 200., 300., 100., 200., 300.]
    reference = folded_volume(constituents(clean))
    longer = folded_volume(constituents(clean + tail, flags=flags))

    assert longer[:len(reference)] == reference


def test_an_untrusted_aggregate_stays_unavailable_downstream():
    """The bins around a flagged constituent must not read as participation."""
    volumes = [100., 200., 300.] * 10
    flags = [False] * 6 + [True] + [False] * 23
    folded = timeframes.fold(constituents(volumes, flags=flags), 3, as_of=LATE)
    enriched = indicators.enrich(folded)

    assert not indicators.has_real_volume(folded)
    assert enriched["rvol"].isna().all()          # not 0.0, not neutral
    # The bad bin and everything after it in the session lose VWAP too.
    assert enriched["vwap"].iloc[2:].isna().all()


def test_a_clean_fold_is_still_the_plain_six_columns():
    """The validity column appears only when it has something to say."""
    clean = timeframes.fold(constituents([100., 200., 300.]), 3, as_of=LATE)
    marked = timeframes.fold(constituents([100., 200., 300.],
                                          flags=[False, True, False]), 3, as_of=LATE)

    assert list(clean.columns) == indicators.REQUIRED_COLS
    assert "volume_is_synthetic" in marked.columns


def test_an_invalid_aggregate_survives_a_six_column_projection():
    """The escape path: metadata disappears, the number must not lie.

    Downstream code routinely selects the six standard columns and copies
    the frame, which drops both the row-level validity column and the
    frame's provenance. If the untrusted bin still held its arithmetic
    sum, that projection handed the next component a confident number on
    a frame that then read as ordinary market data — and VWAP computed
    happily from it.
    """
    folded = timeframes.fold(constituents([100., 200., 300.] * 4,
                                          flags=[False] * 6 + [True] + [False] * 5),
                             3, as_of=LATE)

    projected = folded[indicators.REQUIRED_COLS].copy()
    projected.attrs.clear()                       # provenance gone
    assert "volume_is_synthetic" not in projected.columns

    bad = projected["volume"].isna()
    assert bad.any(), "the fixture must contain an untrusted aggregate"
    assert np.isnan(projected["volume"].iloc[2])

    # Re-declared as genuine by a caller that has lost the history: the
    # value itself still refuses to produce a reading.
    indicators.declare_volume(projected, indicators.GENUINE)
    enriched = indicators.enrich(projected)
    assert not indicators.has_real_volume(projected)
    assert enriched["vwap"].iloc[2:].isna().all()
    assert enriched["rvol"].isna().all()


def test_a_genuine_zero_is_not_lost_by_the_same_rule():
    """The counter-case, so the fix cannot be "NaN everything"."""
    folded = timeframes.fold(constituents([100., 0., 300.]), 3, as_of=LATE)
    projected = folded[indicators.REQUIRED_COLS].copy()

    assert projected["volume"].iloc[0] == 400.0
    assert indicators.has_real_volume(folded)
