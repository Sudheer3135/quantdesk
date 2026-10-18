"""Tests for the NSE trading-holiday calendar.

A holiday calendar fails in two directions and both are expensive:

  - A *missing* holiday makes a clean archive look like an outage. Two
    absent entries (Bakri Id and Muharram 2026) were enough to report the
    whole dataset as unusable, because from the data alone a market holiday
    and a day of lost collection are identical.
  - A *spurious* holiday is worse. Validation rejects candles dated on a
    holiday, so inventing one silently discards a real trading session and
    keeps discarding it on every future import.

The second is what most of this file guards against.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.market_calendar import (
    HOLIDAYS,
    PROVISIONAL_YEARS,
    covered_years,
    is_holiday,
    is_provisional,
    is_session,
    sessions_between,
)

# ---- the two verified additions ---------------------------------------

@pytest.mark.parametrize("day,name", [
    (date(2026, 5, 28), "Bakri Id"),
    (date(2026, 6, 26), "Muharram"),
])
def test_the_verified_2026_holidays_are_recognised(day, name):
    """Both confirmed against the NSE F&O 2026 circular on 17-Aug-2026.
    Before this, each was reported as a missing trading session."""
    assert is_holiday(day) is True, f"{name} on {day} should be a holiday"
    assert is_session(day) is False


@pytest.mark.parametrize("day", [
    date(2026, 5, 27),   # Wed before Bakri Id
    date(2026, 5, 29),   # Fri after
    date(2026, 6, 25),   # Thu before Muharram
    date(2026, 6, 29),   # Mon after (26th is Fri, 27-28 weekend)
])
def test_the_days_around_them_are_still_trading_sessions(day):
    """The failure mode of a careless fix: widening a holiday until it
    swallows a real session, which validation then rejects forever."""
    assert is_session(day) is True, f"{day} is a real trading session"
    assert is_holiday(day) is False


def test_the_gap_between_them_is_otherwise_intact():
    """Every weekday from 27-May to 29-Jun 2026 should be a session except
    the two verified holidays. Anything else means the fix removed a day it
    should not have."""
    sessions, unverified = sessions_between(date(2026, 5, 27), date(2026, 6, 29))
    assert not unverified, "2026 has a holiday list; nothing should be unverified"

    expected_absent = {date(2026, 5, 28), date(2026, 6, 26)}
    day = date(2026, 5, 27)
    while day <= date(2026, 6, 29):
        if day.weekday() < 5:
            if day in expected_absent:
                assert day not in sessions, f"{day} is a verified holiday"
            else:
                assert day in sessions, f"{day} was wrongly removed as a session"
        day += timedelta(days=1)


# ---- the 14-Sep-2026 correction ----------------------------------------

@pytest.mark.parametrize("day,name", [
    (date(2026, 1, 15), "Maharashtra municipal elections"),
    (date(2026, 3, 3), "Holi"),
    (date(2026, 3, 31), "Shri Mahavir Jayanti"),
    (date(2026, 9, 14), "Ganesh Chaturthi"),
    (date(2026, 10, 20), "Dussehra"),
    (date(2026, 11, 24), "Prakash Gurpurb Sri Guru Nanak Dev"),
])
def test_the_holidays_missing_until_14_sep_2026_are_recognised(day, name):
    """Each was absent from the transcription.

    14-Sep is the one that was felt: the desk reported the market OPEN on a
    closed exchange, the agent wrote signals from Friday's frozen candles,
    and a correctly silent Angel feed was reported stale all afternoon.
    Dussehra and Prakash Gurpurb would have done the same thing again."""
    assert is_holiday(day) is True, f"{name} on {day} should be a holiday"
    assert is_session(day) is False


def test_holi_2026_is_the_third_not_the_fourth():
    """The transcription had Holi a day late. That is both failures at once:
    a real holiday reported as a missing session, and a real session on the
    4th rejected by validation on every import."""
    assert is_holiday(date(2026, 3, 3)) is True
    assert is_holiday(date(2026, 3, 4)) is False
    assert is_session(date(2026, 3, 4)) is True


@pytest.mark.parametrize("day", [
    date(2026, 9, 11),   # Fri before Ganesh Chaturthi
    date(2026, 9, 15),   # Tue after — the market reopens
    date(2026, 10, 19),  # Mon before Dussehra
    date(2026, 10, 21),  # Wed after
    date(2026, 11, 23),  # Mon before Prakash Gurpurb
    date(2026, 11, 25),  # Wed after
])
def test_the_sessions_around_the_new_holidays_survive(day):
    """A careless fix widens a holiday until it swallows a real session."""
    assert is_session(day) is True, f"{day} is a real trading session"


def test_2026_has_the_full_published_count():
    """Fifteen weekday closures from the annual circular, one added by a
    later notice, plus Independence Day on a Saturday."""
    days = HOLIDAYS[2026]
    assert len([d for d in days if d.weekday() < 5]) == 16
    assert len(days) == 17


# ---- guards against a bad calendar ------------------------------------

def test_weekend_dated_holidays_are_rare_and_change_nothing():
    """A dated holiday can legitimately fall on a weekend — Independence Day
    is 15 August whatever day that is, and in 2026 it is a Saturday. Such an
    entry is redundant rather than wrong, so it is allowed.

    What would be wrong is weekends leaking into the list wholesale, which
    is what the ratio guards against. And either way the entry must not
    change behaviour: the day is already not a session because it is a
    weekend.
    """
    for year, days in HOLIDAYS.items():
        weekend = [d for d in days if d.weekday() >= 5]
        assert len(weekend) <= 3, (
            f"{year} lists {len(weekend)} weekend dates as holidays, which "
            "suggests weekends are leaking into the list rather than a "
            "fixed-date holiday happening to fall on one")
        for day in weekend:
            assert is_session(day) is False


def test_every_holiday_sits_in_the_year_that_keys_it():
    for year, days in HOLIDAYS.items():
        for day in days:
            assert day.year == year, f"{day} is filed under {year}"


def test_each_covered_year_has_a_plausible_number_of_holidays():
    """NSE publishes roughly 10–20 trading holidays a year. A year with two
    is a truncated transcription; a year with forty is weekends leaking in.
    Neither would be obvious from any single query."""
    for year in covered_years():
        count = len(HOLIDAYS[year])
        assert 8 <= count <= 25, f"{year} has {count} holidays, which is implausible"


def test_a_year_with_no_list_returns_unknown_rather_than_false():
    """Three-valued on purpose. `bool(is_holiday(d))` would turn "I don't
    know" into "it was a trading day", which is the exact assumption this
    design exists to prevent."""
    assert is_holiday(date(2019, 6, 17)) is None
    assert is_session(date(2019, 6, 17)) is None


def test_a_weekend_is_false_even_in_an_uncovered_year():
    """Weekends need no calendar."""
    assert is_session(date(2019, 6, 15)) is False      # Saturday


def test_2026_remains_flagged_provisional():
    """Two of its ten entries were verified; the other eight were not, and
    a year is only as trustworthy as its least-checked date. Promoting the
    whole year on the strength of two confirmations is exactly the silent
    assumption this module refuses to make."""
    assert 2026 in PROVISIONAL_YEARS
    assert is_provisional(date(2026, 5, 28))


def test_provisional_years_still_answer_definitively():
    """The caveat must not weaken the check. A provisional year still
    returns True/False, so a genuinely missing session is still an error
    rather than excused as 'we can't be sure'."""
    assert is_holiday(date(2026, 1, 26)) is True     # Republic Day
    assert is_holiday(date(2026, 6, 17)) is False    # ordinary Wednesday
    assert is_session(date(2026, 6, 17)) is True


def test_sessions_between_excludes_weekends_and_holidays():
    sessions, _ = sessions_between(date(2026, 8, 14), date(2026, 8, 17))
    assert date(2026, 8, 14) in sessions      # Friday
    assert date(2026, 8, 15) not in sessions  # Saturday, and Independence Day
    assert date(2026, 8, 16) not in sessions  # Sunday
    assert date(2026, 8, 17) in sessions      # Monday
