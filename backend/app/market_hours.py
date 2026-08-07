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

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


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


def is_open(moment: datetime | None = None) -> bool:
    local = to_ist(moment)
    return is_trading_day(local) and MARKET_OPEN <= local.time() <= MARKET_CLOSE


def session_label(moment: datetime | None = None) -> str:
    local = to_ist(moment)
    if not is_trading_day(local):
        return "weekend"
    if local.time() < MARKET_OPEN:
        return "pre-open"
    if local.time() > MARKET_CLOSE:
        return "closed"
    return "open"


def status(moment: datetime | None = None) -> dict:
    """What the dashboard needs to say whether it is showing live analysis.

    A signal computed at 16:40 on the day's final candle is a closing read,
    not a tradeable setup. The interface should be able to say so rather
    than presenting stale analysis as if the market were open.
    """
    local = to_ist(moment)
    return {
        "open": is_open(local),
        "session": session_label(local),
        "server_time": local.isoformat(),
    }


def trading_date(moment: datetime | None = None) -> date:
    return to_ist(moment).date()