"""NSE session hours — the single source of truth.

This lived in three places at once: the agent, the price ticker and the
websocket stream each had their own copy of the open and close times. Three
copies of a rule is three chances for them to disagree, and the first
symptom would have been the dashboard calling the market open while the
agent had already stopped for the day.

Deliberately dependency-free — standard library only. Anything here can be
imported and tested without a database, a broker or a settings file.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from . import market_calendar

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# When the desk starts calling the session imminent. The project had no
# pre-open definition — `session_label` returned "pre-open" for every minute
# of a weekday before 09:15, so 00:02 on a Friday read as pre-open. NSE's own
# call-auction runs 09:00-09:15; this window is deliberately a little wider so
# the dashboard warns before the auction rather than at it. One constant to
# change if you want it tighter.
PRE_OPEN_MINUTES = 30
PRE_OPEN = (datetime.combine(date(2000, 1, 1), MARKET_OPEN)
            - timedelta(minutes=PRE_OPEN_MINUTES)).time()

# The three states the desk can be in. Weekend and holiday are both CLOSED;
# which one is reported separately by `closed_reason`.
CLOSED, PRE_OPEN_STATE, OPEN = "closed", "pre-open", "open"

# Bound on the search for the next session, so a bad calendar cannot spin.
# Comfortably longer than any NSE holiday run.
_MAX_LOOKAHEAD_DAYS = 30


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(moment: datetime | None = None) -> datetime:
    return (moment or now_ist()).astimezone(IST)


def is_trading_day(moment: datetime | None = None) -> bool:
    """Weekdays only.

    Exchange holidays are not handled — that needs a published calendar, and
    guessing at one would be worse than not claiming to know. On a holiday
    this returns True and the data source simply returns nothing new, which
    is a harmless outcome.
    """
    return to_ist(moment).weekday() < 5


def is_trading_date(day: date) -> bool:
    """Does the market trade on this date, holidays included?

    Reuses `market_calendar` rather than restating the rule. The calendar
    answers None for a year it has no list for; that falls back to
    weekdays-only, which is exactly what its own docstring prescribes.

    Deliberately NOT wired into `is_open` below. That function gates the
    option collector, the agent and the importer's out-of-hours data-quality
    check, and the 2026 holiday list is still marked provisional. A
    mis-transcribed holiday that only affects the countdown costs a wrong
    label for a day; one that gates collection costs a day of option
    snapshots, and option history cannot be backfilled. So the calendar
    informs what the desk *says*, not what it *records*.
    """
    known = market_calendar.is_session(day)
    if known is None:
        return day.weekday() < 5
    return known


def is_open(moment: datetime | None = None) -> bool:
    local = to_ist(moment)
    return is_trading_day(local) and MARKET_OPEN <= local.time() <= MARKET_CLOSE


def session_label(moment: datetime | None = None) -> str:
    """Which of the three states the market is in right now.

    The bug this replaces: any weekday minute before 09:15 returned
    "pre-open", so midnight on a Friday claimed the session was imminent and
    the dashboard counted toward an open nine hours away as though it were
    minutes. Pre-open is now a bounded window, and a holiday reads closed
    rather than pre-open.
    """
    local = to_ist(moment)
    if not is_trading_date(local.date()):
        return CLOSED
    now = local.time()
    if now < PRE_OPEN:
        return CLOSED
    if now < MARKET_OPEN:
        return PRE_OPEN_STATE
    if now < MARKET_CLOSE:
        return OPEN
    return CLOSED


def closed_reason(moment: datetime | None = None) -> str | None:
    """Why the market is shut, or None while it is trading.

    Kept separate from the state so the badge can stay a clean three-way
    while the caption still explains itself.
    """
    local = to_ist(moment)
    day = local.date()
    if day.weekday() >= 5:
        return "weekend"
    if market_calendar.is_holiday(day):
        return "holiday"
    now = local.time()
    if now < PRE_OPEN:
        return "before-open"
    if now > MARKET_CLOSE:
        return "after-close"
    return None


def next_open(moment: datetime | None = None) -> datetime:
    """The next moment the market opens, as an absolute IST instant.

    Today counts only if today trades and the open has not passed. Otherwise
    it walks forward over weekends and holidays to the next real session —
    which is why Friday evening points at Monday, not Saturday.
    """
    local = to_ist(moment)
    if is_trading_date(local.date()) and local.time() < MARKET_OPEN:
        return datetime.combine(local.date(), MARKET_OPEN, tzinfo=IST)

    day = local.date()
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        day += timedelta(days=1)
        if is_trading_date(day):
            return datetime.combine(day, MARKET_OPEN, tzinfo=IST)

    raise RuntimeError(
        f"no trading day found within {_MAX_LOOKAHEAD_DAYS} days of {local.date()}; "
        "the holiday calendar is probably wrong")


def next_close(moment: datetime | None = None) -> datetime:
    """The close of the session the market is currently in.

    Only meaningful while open; callers use `next_open` otherwise.
    """
    local = to_ist(moment)
    return datetime.combine(local.date(), MARKET_CLOSE, tzinfo=IST)


def status(moment: datetime | None = None) -> dict:
    """What the dashboard needs to say whether it is showing live analysis.

    A signal computed at 16:40 on the day's final candle is a closing read,
    not a tradeable setup. The interface should be able to say so rather
    than presenting stale analysis as if the market were open.
    """
    local = to_ist(moment)
    state = session_label(local)

    # One boundary, computed once here. The dashboard formats it and does
    # not decide anything itself — two places deciding whether the market is
    # open is how they end up disagreeing on a holiday.
    if state == OPEN:
        boundary, direction = next_close(local), "closes"
    else:
        boundary, direction = next_open(local), "opens"

    return {
        "open": state == OPEN,
        "session": state,
        "reason": closed_reason(local),
        "server_time": local.isoformat(),
        "next_open": next_open(local).isoformat(),
        # Absolute instant, so a browser counts down against a fixed target
        # instead of accumulating drift from its own timer.
        "next_boundary": boundary.isoformat(),
        "boundary_direction": direction,
        "seconds_to_boundary": max(0, int((boundary - local).total_seconds())),
        # The 2026 list is transcribed but unverified; say so rather than
        # presenting a guessed session date as settled.
        "calendar_provisional": market_calendar.is_provisional(next_open(local).date()),
    }


def trading_date(moment: datetime | None = None) -> date:
    return to_ist(moment).date()
