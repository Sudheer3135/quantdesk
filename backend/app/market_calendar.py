"""NSE trading holidays.

`market_hours.is_trading_day` knows about weekends and says so honestly. It
does not know about Diwali. That gap matters here and almost nowhere else:
the missing-candle diagnostic compares stored bars against the sessions that
should exist, and without a holiday list every exchange holiday is reported
as a data outage. A diagnostic that cries wolf a dozen times a year is a
diagnostic nobody reads by March.

The design rule for this module is that **not knowing is a valid answer**.
`is_holiday` returns None for any year it does not carry, and every caller
must degrade to weekday-only rather than assert a holiday it cannot know
about. A stale calendar silently claiming 2027 has no holidays would be
worse than no calendar at all, because the resulting false "missing
session" findings look identical to real ones.

Maintaining this: NSE publishes the list by circular each December, at
https://www.nseindia.com/resources/exchange-communication-holidays. Add the
year to HOLIDAYS, drop it from PROVISIONAL_YEARS once checked against the
circular, and move VERIFIED_THROUGH forward.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

# Trading holidays for the equity and F&O segments — days the market is
# shut. Muhurat trading sessions are deliberately excluded: they are a
# single evening hour, they do not produce a normal 09:15–15:30 session, and
# treating one as a full trading day would report 74 of 75 bars missing.
HOLIDAYS: dict[int, frozenset[date]] = {
    2024: frozenset({
        date(2024, 1, 26), date(2024, 3, 8), date(2024, 3, 25),
        date(2024, 3, 29), date(2024, 4, 11), date(2024, 4, 17),
        date(2024, 5, 1), date(2024, 5, 20), date(2024, 6, 17),
        date(2024, 7, 17), date(2024, 8, 15), date(2024, 10, 2),
        date(2024, 11, 1), date(2024, 11, 15), date(2024, 12, 25),
    }),
    2025: frozenset({
        date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
        date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
        date(2025, 5, 1), date(2025, 8, 15), date(2025, 8, 27),
        date(2025, 10, 2), date(2025, 10, 21), date(2025, 10, 22),
        date(2025, 11, 5), date(2025, 12, 25),
    }),
    2026: frozenset({
        date(2026, 1, 26), date(2026, 3, 4), date(2026, 3, 26),
        date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1),
        date(2026, 8, 15), date(2026, 10, 2), date(2026, 11, 10),
        date(2026, 12, 25),
    }),
}

# Years transcribed but not yet checked line-by-line against the NSE
# circular. Findings derived from these carry a caveat rather than being
# presented as fact. Removing a year from this set is an assertion that
# somebody actually verified it.
PROVISIONAL_YEARS: frozenset[int] = frozenset({2026})

VERIFIED_THROUGH = date(2025, 12, 31)

_warned: set[int] = set()


def is_holiday(day: date) -> bool | None:
    """True, False, or None when the year is not covered.

    Callers must handle None explicitly. `bool(is_holiday(d))` silently
    turns "I don't know" into "it was a trading day", which is exactly the
    failure this three-valued return exists to prevent.
    """
    holidays = HOLIDAYS.get(day.year)
    if holidays is None:
        if day.year not in _warned:
            _warned.add(day.year)
            log.info(
                "no NSE holiday list for %s — session checks for that year "
                "fall back to weekdays only", day.year)
        return None
    return day in holidays


def is_provisional(day: date) -> bool:
    """Is this year's list transcribed but unverified?"""
    return day.year in PROVISIONAL_YEARS


def covered_years() -> list[int]:
    return sorted(HOLIDAYS)


def is_session(day: date) -> bool | None:
    """Was the market open on this date? None when the year is unknown.

    A weekend is a definite no regardless of the calendar, which is why
    that check comes first.
    """
    if day.weekday() >= 5:
        return False
    holiday = is_holiday(day)
    if holiday is None:
        return None
    return not holiday


def sessions_between(start: date, end: date) -> tuple[list[date], list[date]]:
    """Trading days in [start, end], split into known and unverified.

    Returns (sessions, unverified). `unverified` holds the weekdays whose
    year has no holiday list — they are probably sessions, but the caller
    should label anything derived from them rather than assert it.
    """
    sessions: list[date] = []
    unverified: list[date] = []
    day = start
    while day <= end:
        state = is_session(day)
        if state is True:
            sessions.append(day)
        elif state is None:
            unverified.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return sessions, unverified
