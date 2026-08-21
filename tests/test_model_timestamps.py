"""`created_at` defaults are timezone-aware.

The three core tables defaulted to a *naive* UTC value going into a
TIMESTAMP WITH TIME ZONE column. Postgres reads such a value in the session
timezone, so the instant that landed in the database was correct only
because that session happened to be UTC. Point `TimeZone` at Asia/Kolkata —
an entirely reasonable thing to do on an Indian trading system — and every
row shifts five and a half hours.

`TradeRecord.created_at` is what `todays_trades()` filters on, which is what
the daily trade cap counts. A five-and-a-half-hour shift moves trades across
the IST midnight boundary in both directions: yesterday's last trade starts
occupying today's cap, and this morning's stops counting.

These assertions are deliberately about the value Python produces, not about
what a particular server hands back — that is what makes them independent of
any session timezone, and it is where the defect actually lived.
"""
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import repository
from app.market_hours import IST, trading_date
from app.models import DatasetVersion, SignalRecord, TradeRecord, utc_now

# The brief named this third one "DatasetRecord"; the class is
# `DatasetVersion`. Same table, same defaulted column.
TIMESTAMPED = [SignalRecord, TradeRecord, DatasetVersion]


# ---- the default itself -----------------------------------------------

def test_the_default_is_timezone_aware():
    now = utc_now()
    assert now.tzinfo is not None, "naive datetime — the defect this replaces"
    assert now.utcoffset() == timedelta(0)


def test_the_default_is_actually_now():
    """A guard that returned a fixed aware datetime would pass the test above
    and be catastrophically wrong."""
    before = datetime.now(UTC)
    sampled = utc_now()
    after = datetime.now(UTC)
    assert before <= sampled <= after


@pytest.mark.parametrize("model", TIMESTAMPED, ids=lambda m: m.__name__)
def test_every_core_table_defaults_to_an_aware_instant(model):
    """Named individually so adding a fourth table with the old spelling is
    a visible omission rather than a silent one.

    Asserted by calling the default rather than by comparing it to
    `utc_now`: SQLAlchemy wraps a zero-argument callable, so the function on
    the column is not the one that was handed to it. Behaviour is what
    matters here anyway — the old default was wrong because of what it
    returned, not because of its name.
    """
    default = model.__table__.c.created_at.default
    assert default is not None, f"{model.__name__}.created_at has no default"
    assert default.is_callable

    produced = default.arg(None)
    assert produced.tzinfo is not None, "naive default — the defect this replaces"
    assert produced.utcoffset() == timedelta(0)


def test_the_naive_helper_would_fail_that_check():
    """Guard against the guard. If an aware datetime were not actually
    distinguishable here, every assertion above would pass vacuously."""
    assert datetime.utcnow().tzinfo is None      # noqa: DTZ003 - the point


@pytest.mark.parametrize("model", TIMESTAMPED, ids=lambda m: m.__name__)
def test_the_column_is_declared_timezone_aware(model):
    assert model.__table__.c.created_at.type.timezone is True


# ---- what actually reaches the row ------------------------------------

def test_an_inserted_row_carries_an_aware_instant(db):
    """Independent of the backend's own timezone handling: this is the value
    the ORM applied, read back before the session expires it. SQLite would
    hand a naive datetime back on a later refresh, which is exactly why the
    assertion is placed here."""
    record = TradeRecord(symbol="NIFTY", side="BUY", quantity=75,
                         entry=24_200.0, stop_loss=24_190.0, status="open")
    db.add(record)
    db.flush()

    assert record.created_at.tzinfo is not None
    assert record.created_at.utcoffset() == timedelta(0)


def test_the_instant_is_right_whatever_zone_it_is_rendered_in(db):
    """The point of storing an instant rather than a wall clock."""
    record = SignalRecord(symbol="NIFTY", timeframe="5m", action="HOLD",
                          confidence=0.2, price=24_200.0)
    db.add(record)
    db.flush()

    stamped = record.created_at
    assert stamped.astimezone(IST).utcoffset() == timedelta(hours=5, minutes=30)
    assert abs((datetime.now(UTC) - stamped).total_seconds()) < 60


# ---- the cap still counts the right day -------------------------------

def test_trading_day_filtering_is_unchanged(db):
    """The behaviour the fix must not disturb."""
    now = datetime.now(IST)
    db.add(TradeRecord(symbol="NIFTY", side="BUY", quantity=75, entry=24_200.0,
                       stop_loss=24_190.0, status="closed", pnl=100.0,
                       created_at=now.astimezone(UTC)))
    db.commit()

    assert len(repository.todays_trades(db, trading_date())) == 1


def test_a_default_stamped_trade_lands_on_todays_trading_date(db):
    """End to end: no explicit `created_at`, so the new default is what the
    cap will be counting."""
    db.add(TradeRecord(symbol="NIFTY", side="BUY", quantity=75,
                       entry=24_200.0, stop_loss=24_190.0, status="open"))
    db.commit()

    assert len(repository.todays_trades(db, trading_date())) == 1


def test_yesterdays_trade_does_not_occupy_todays_cap(db):
    db.add(TradeRecord(symbol="NIFTY", side="BUY", quantity=75, entry=24_200.0,
                       stop_loss=24_190.0, status="closed", pnl=50.0,
                       created_at=(datetime.now(IST) - timedelta(days=1)).astimezone(UTC)))
    db.commit()

    assert repository.todays_trades(db, trading_date()) == []


def test_a_trade_written_from_another_zone_still_lands_on_the_right_day(db):
    """The failure mode the naive default created, stated directly.

    A trade at 09:20 IST is 03:50 UTC the same day. Whichever zone the caller
    expressed it in, the trading date must come out the same — that is what
    goes wrong when a naive local value is stored in a tz-aware column.
    """
    morning_ist = datetime.now(IST).replace(hour=9, minute=20, second=0, microsecond=0)
    for zone in (UTC, IST, timezone(timedelta(hours=-5))):
        db.query(TradeRecord).delete()
        db.add(TradeRecord(symbol="NIFTY", side="BUY", quantity=75,
                           entry=24_200.0, stop_loss=24_190.0, status="open",
                           created_at=morning_ist.astimezone(zone)))
        db.commit()

        found = repository.todays_trades(db, morning_ist.date())
        assert len(found) == 1, f"lost the trade when written as {zone}"
