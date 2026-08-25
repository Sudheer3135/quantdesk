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

from app.analytics import timeframes
from app.market_hours import IST

BARS_PER_SESSION = 75          # 09:15 to 15:25, five minutes apart


def five_minute(sessions=2, bars=BARS_PER_SESSION, start=24_000.0, step=1.0):
    """A clean ramp, so every aggregate is arithmetic anyone can check."""
    stamps, day = [], datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    for _ in range(sessions):
        for i in range(bars):
            stamps.append((day + timedelta(minutes=5 * i)).astimezone(UTC))
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    n = len(stamps)
    close = start + np.arange(n) * step
    return pd.DataFrame({
        "timestamp": stamps, "open": close - step,
        "high": close + 2, "low": close - 3,
        "close": close, "volume": [100.0] * n})


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
    assert len(timeframes.hourly(df)) == 6


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


def test_the_current_sessions_stub_is_still_held_back():
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

    assert ist_times(folded)[-1] == "14:15"
    assert folded["close"].iloc[-1] != df["close"].iloc[-1]

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


def test_synthetic_volume_stays_detectable_after_folding():
    """Summing a constant gives another constant, so `has_real_volume` must
    still say no — otherwise the bias layer would start trusting a
    placeholder it could not see through."""
    from app.analytics import indicators

    df = five_minute()
    assert not indicators.has_real_volume(df)
    assert not indicators.has_real_volume(timeframes.fifteen_minute(df))


def test_a_nonsense_group_size_is_refused():
    with pytest.raises(ValueError):
        timeframes.fold(five_minute(), 0)
