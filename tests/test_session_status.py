"""Market session state, the next session, and the countdown.

The bug: `session_label` returned "pre-open" for every weekday minute before
09:15. At 00:02 on a Friday the desk announced the session was imminent and
counted toward an open nine hours away, and a holiday looked identical to a
normal morning.

These tests assert on datetimes and state strings, never on formatted text —
a countdown that reads correctly while pointing at the wrong day is exactly
the failure being fixed.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.market_hours import (
    IST,
    MARKET_OPEN,
    PRE_OPEN,
    closed_reason,
    is_trading_date,
    next_open,
    session_label,
    status,
)

# 2026-08-21 is a Friday and not an NSE holiday.
FRIDAY = date(2026, 8, 21)
SATURDAY = date(2026, 8, 22)
SUNDAY = date(2026, 8, 23)
MONDAY = date(2026, 8, 24)


def ist(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


# ---------------------------------------------------------------------------
# THE SCREENSHOT
# ---------------------------------------------------------------------------

def test_friday_at_two_minutes_past_midnight_is_closed_not_pre_open():
    """The exact reported case.

    Asserted on the underlying values, not the badge text: state, the next
    session instant, and that pre-open is false.
    """
    when = ist(FRIDAY, 0, 2)
    s = status(when)

    assert s["session"] == "closed"
    assert s["open"] is False
    assert s["session"] != "pre-open"
    assert s["reason"] == "before-open"

    # Next session is this same Friday's open, not tomorrow and not today's
    # already-passed one.
    assert datetime.fromisoformat(s["next_open"]) == ist(FRIDAY, 9, 15)

    # ~9h13m away.
    assert s["boundary_direction"] == "opens"
    assert s["seconds_to_boundary"] == pytest.approx(9 * 3600 + 13 * 60, abs=60)


# ---------------------------------------------------------------------------
# 1-7: the states through a trading day
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hh,mm,expected", [
    (0, 2, "closed"),
    (6, 0, "closed"),
    (8, 30, "closed"),
    (8, 44, "closed"),
    (8, 45, "pre-open"),      # the window opens
    (8, 59, "pre-open"),
    (9, 14, "pre-open"),
    (9, 15, "open"),
    (12, 0, "open"),
    (15, 29, "open"),
    (15, 30, "closed"),       # the close itself ends the session
    (15, 31, "closed"),
    (23, 59, "closed"),
])
def test_session_state_through_a_trading_day(hh, mm, expected):
    assert session_label(ist(FRIDAY, hh, mm)) == expected


def test_pre_open_is_a_bounded_window_not_everything_before_the_open():
    """The regression in one assertion."""
    assert session_label(ist(FRIDAY, 0, 2)) == "closed"
    assert session_label(ist(FRIDAY, 8, 59)) == "pre-open"
    assert PRE_OPEN < MARKET_OPEN
    # Midnight must be outside the window by hours, not minutes.
    assert PRE_OPEN.hour >= 8


# ---------------------------------------------------------------------------
# 8-10: weekends roll to the next real session
# ---------------------------------------------------------------------------

def test_friday_night_points_at_monday():
    s = status(ist(FRIDAY, 18, 0))
    assert s["session"] == "closed"
    assert s["reason"] == "after-close"
    assert datetime.fromisoformat(s["next_open"]) == ist(MONDAY, 9, 15)


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_weekend_is_closed_and_points_at_monday(day):
    s = status(ist(day, 12, 0))
    assert s["session"] == "closed"
    assert s["open"] is False
    assert s["reason"] == "weekend"
    assert datetime.fromisoformat(s["next_open"]) == ist(MONDAY, 9, 15)


def test_saturday_is_never_pre_open():
    """A weekend morning has no pre-open, whatever the clock says."""
    for hh, mm in ((8, 50), (9, 0), (9, 14)):
        assert session_label(ist(SATURDAY, hh, mm)) == "closed"


# ---------------------------------------------------------------------------
# 11: holidays
# ---------------------------------------------------------------------------

def test_a_holiday_is_closed_and_skipped_when_finding_the_next_session():
    from app.market_calendar import HOLIDAYS

    holiday = next(d for d in sorted(HOLIDAYS[2026])
                   if d.weekday() < 4 and d > date(2026, 1, 1))

    assert is_trading_date(holiday) is False
    assert session_label(ist(holiday, 11, 0)) == "closed"
    assert closed_reason(ist(holiday, 11, 0)) == "holiday"

    # The day before it must skip over it rather than name it.
    previous = holiday - timedelta(days=1)
    if is_trading_date(previous):
        after_close = ist(previous, 16, 0)
        assert next_open(after_close).date() != holiday
        assert is_trading_date(next_open(after_close).date())


def test_next_open_never_lands_on_a_non_trading_day():
    """Walk a full year; every answer must itself be a session."""
    day = date(2026, 1, 1)
    for _ in range(365):
        target = next_open(ist(day, 20, 0))
        assert is_trading_date(target.date()), target
        assert target.time() == MARKET_OPEN
        day += timedelta(days=1)


# ---------------------------------------------------------------------------
# 12: timezone conversion
# ---------------------------------------------------------------------------

def test_utc_input_is_converted_to_ist_before_being_judged():
    """18:32 UTC on Thursday is 00:02 IST on Friday.

    Reading the calendar date off the UTC value would call it Thursday and
    could report the session as already open.
    """
    from datetime import UTC

    utc = datetime(2026, 8, 20, 18, 32, tzinfo=UTC)
    assert utc.astimezone(IST) == ist(FRIDAY, 0, 2)

    s = status(utc)
    assert s["session"] == "closed"
    assert datetime.fromisoformat(s["next_open"]) == ist(FRIDAY, 9, 15)
    assert datetime.fromisoformat(s["server_time"]).utcoffset() == timedelta(hours=5, minutes=30)


def test_naive_free_comparison_across_zones_agrees():
    """The same instant expressed two ways gives the same state."""
    from datetime import UTC

    ist_moment = ist(FRIDAY, 12, 0)
    utc_moment = ist_moment.astimezone(UTC)
    assert status(ist_moment)["session"] == status(utc_moment)["session"] == "open"
    assert status(ist_moment)["next_boundary"] == status(utc_moment)["next_boundary"]


# ---------------------------------------------------------------------------
# 13-16: the countdown
# ---------------------------------------------------------------------------

def test_countdown_before_the_open_targets_todays_open():
    s = status(ist(FRIDAY, 8, 0))
    assert s["boundary_direction"] == "opens"
    assert datetime.fromisoformat(s["next_boundary"]) == ist(FRIDAY, 9, 15)
    assert s["seconds_to_boundary"] == 75 * 60


def test_countdown_during_the_session_targets_the_close():
    s = status(ist(FRIDAY, 10, 18))
    assert s["boundary_direction"] == "closes"
    assert datetime.fromisoformat(s["next_boundary"]) == ist(FRIDAY, 15, 30)
    assert s["seconds_to_boundary"] == (5 * 3600 + 12 * 60)


def test_countdown_after_the_close_targets_the_next_session():
    s = status(ist(FRIDAY, 16, 0))
    assert s["boundary_direction"] == "opens"
    assert datetime.fromisoformat(s["next_boundary"]) == ist(MONDAY, 9, 15)
    assert s["seconds_to_boundary"] == (65 * 3600 + 15 * 60)


def test_countdown_decreases_as_time_passes_and_never_grows():
    """The reported symptom was a timer that climbed. Walk a whole day at
    one-minute steps: within a state the countdown may only fall."""
    previous = None
    previous_boundary = None
    for minute in range(0, 24 * 60, 1):
        moment = ist(FRIDAY, 0, 0) + timedelta(minutes=minute)
        s = status(moment)
        boundary = s["next_boundary"]
        seconds = s["seconds_to_boundary"]
        if previous is not None and boundary == previous_boundary:
            assert seconds < previous, f"countdown grew at {moment}"
        previous, previous_boundary = seconds, boundary


def test_countdown_is_anchored_to_an_absolute_instant():
    """The browser counts against this, so it must be a fixed target rather
    than a duration that has to be refreshed to stay true."""
    early = status(ist(FRIDAY, 1, 0))
    later = status(ist(FRIDAY, 5, 0))
    assert early["next_boundary"] == later["next_boundary"]
    assert later["seconds_to_boundary"] < early["seconds_to_boundary"]


# ---------------------------------------------------------------------------
# 17: no stale state
# ---------------------------------------------------------------------------

def test_status_is_recomputed_per_call_and_holds_nothing():
    """Two different moments must never return the same answer from a cache."""
    a = status(ist(FRIDAY, 8, 0))
    b = status(ist(FRIDAY, 12, 0))
    c = status(ist(FRIDAY, 8, 0))

    assert a["session"] == "closed"
    assert b["session"] == "open"
    assert a == c                      # pure function of its argument


def test_status_with_no_argument_uses_the_current_clock():
    from app.market_hours import now_ist

    s = status()
    served = datetime.fromisoformat(s["server_time"])
    assert abs((served - now_ist()).total_seconds()) < 5


def test_the_open_flag_agrees_with_the_session_label():
    for hh, mm in ((0, 2), (8, 50), (9, 15), (12, 0), (15, 30), (16, 0)):
        s = status(ist(FRIDAY, hh, mm))
        assert s["open"] is (s["session"] == "open")


def test_provisional_calendar_is_declared():
    """2026's list is transcribed but unverified; the payload must say so
    rather than presenting a guessed session date as settled."""
    s = status(ist(FRIDAY, 0, 2))
    assert s["calendar_provisional"] is True


def test_boundary_and_seconds_stay_consistent():
    for hh in range(0, 24):
        moment = ist(FRIDAY, hh, 0)
        s = status(moment)
        boundary = datetime.fromisoformat(s["next_boundary"])
        assert s["seconds_to_boundary"] == max(0, int((boundary - moment).total_seconds()))
