"""Tests for the historical import path.

The property that matters most here is idempotency. A backfill that is not
safe to re-run is a backfill you hesitate to run, and hesitating is how you
end up with gaps.
"""
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data.importer import import_index_candles
from app.data.validation import clean_candles, impossible_mask
from app.models import CandleRecord

IST = timezone(timedelta(hours=5, minutes=30))


def session_bars(day: date, count: int = 12, start_price: float = 24_000.0,
                 real_volume: bool = True) -> pd.DataFrame:
    """`count` five-minute bars from 09:15 IST on `day`, all valid."""
    first = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    rows = []
    for i in range(count):
        ts = first + timedelta(minutes=5 * i)
        base = start_price + i
        rows.append({
            "timestamp": pd.Timestamp(ts).tz_convert("UTC"),
            "open": base, "high": base + 5, "low": base - 5, "close": base + 2,
            "volume": (1000 + i * 7) if real_volume else 1.0,
        })
    return pd.DataFrame(rows)


def count_rows(db) -> int:
    return db.scalar(select(func.count()).select_from(CandleRecord))


# ---- idempotency ------------------------------------------------------

def test_importing_the_same_window_twice_writes_no_new_rows(db):
    """The whole point of the importer. If this fails, a scheduled backfill
    silently doubles the dataset and every statistic derived from it."""
    df = session_bars(date(2026, 6, 16))

    first = import_index_candles(db, df, "NIFTY", "5m", "test")
    assert first.write.inserted == 12
    assert first.write.updated == 0
    assert count_rows(db) == 12

    second = import_index_candles(db, df, "NIFTY", "5m", "test")
    assert second.write.inserted == 0
    assert second.write.updated == 12
    assert count_rows(db) == 12


def test_overlapping_windows_do_not_duplicate(db):
    """Backfills overlap by design — you re-pull the last few days to catch
    restatements. Overlap must converge, not accumulate."""
    monday, tuesday = date(2026, 6, 16), date(2026, 6, 17)
    import_index_candles(db, session_bars(monday), "NIFTY", "5m", "test")
    import_index_candles(
        db, pd.concat([session_bars(monday), session_bars(tuesday)]),
        "NIFTY", "5m", "test")
    assert count_rows(db) == 24


def test_duplicate_timestamps_inside_one_batch_are_collapsed(db):
    """Postgres refuses to update the same row twice in one statement, so a
    repeated timestamp inside a single payload must be collapsed before the
    insert rather than relied on to conflict cleanly."""
    df = session_bars(date(2026, 6, 16), count=3)
    doubled = pd.concat([df, df]).reset_index(drop=True)

    report = import_index_candles(db, doubled, "NIFTY", "5m", "test")
    assert report.write.deduplicated == 3
    assert count_rows(db) == 3


def test_a_restated_bar_updates_in_place_and_bumps_revision(db):
    """When a source restates a bar, the archive should hold the new values
    and be able to say that it happened. A bar that keeps being restated is
    worth looking at, and without the counter there is nothing to look at."""
    day = date(2026, 6, 16)
    import_index_candles(db, session_bars(day, count=2), "NIFTY", "5m", "test")

    restated = session_bars(day, count=2)
    restated.loc[0, ["high", "close"]] = [24_060.0, 24_050.0]
    import_index_candles(db, restated, "NIFTY", "5m", "test")

    row = db.scalars(
        select(CandleRecord).order_by(CandleRecord.timestamp)).first()
    assert row.close == 24_050.0
    assert row.revision == 1
    assert count_rows(db) == 2


def test_a_restatement_that_breaks_a_bar_is_rejected_not_stored(db):
    """A restatement is still data from a source, and gets the same
    scrutiny as the original. Trusting an update more than an insert is how
    a good row becomes a bad one."""
    day = date(2026, 6, 16)
    import_index_candles(db, session_bars(day, count=2), "NIFTY", "5m", "test")

    broken = session_bars(day, count=2)
    broken.loc[0, "close"] = 99_999.0          # close far above the high
    report = import_index_candles(db, broken, "NIFTY", "5m", "test")

    assert report.rejection.counts["impossible_ohlc"] == 1
    row = db.scalars(
        select(CandleRecord).order_by(CandleRecord.timestamp)).first()
    assert row.close == 24_002.0               # untouched


# ---- provenance -------------------------------------------------------

def test_source_is_required_and_preserved(db):
    """Requirement 6. The column exists so a mixed archive can be filtered
    back to trustworthy rows."""
    with pytest.raises(TypeError):
        import_index_candles(db, session_bars(date(2026, 6, 16)), "NIFTY", "5m")

    import_index_candles(db, session_bars(date(2026, 6, 16)), "NIFTY", "5m", "yahoo")
    assert {r.source for r in db.scalars(select(CandleRecord))} == {"yahoo"}


def test_synthetic_volume_is_recorded_on_the_row(db):
    """Yahoo publishes no volume for ^NSEI and the free broker substitutes a
    constant. Those rows must not be indistinguishable from real ones."""
    import_index_candles(
        db, session_bars(date(2026, 6, 16), real_volume=False),
        "NIFTY", "5m", "free")
    assert all(r.volume_is_synthetic for r in db.scalars(select(CandleRecord)))

    import_index_candles(
        db, session_bars(date(2026, 6, 17), real_volume=True),
        "NIFTY", "5m", "kite")
    later = db.scalars(
        select(CandleRecord).where(CandleRecord.session_date == date(2026, 6, 17))).all()
    assert later and not any(r.volume_is_synthetic for r in later)


def test_session_date_is_the_ist_trading_day(db):
    """A 09:15 IST bar is 03:45 UTC the same day; a 15:25 IST bar is 09:55
    UTC. Both belong to the same session, and storing the UTC date would
    split some sessions across two dates."""
    import_index_candles(db, session_bars(date(2026, 6, 16), count=75),
                         "NIFTY", "5m", "test")
    assert {r.session_date for r in db.scalars(select(CandleRecord))} == {date(2026, 6, 16)}


# ---- the guards -------------------------------------------------------

def test_future_dated_bars_never_reach_the_table(db):
    """Requirement 5. A future-dated row is the one corruption that is
    invisible afterwards — it looks exactly like real data."""
    future = datetime.now(UTC) + timedelta(days=3)
    df = pd.DataFrame([{
        "timestamp": pd.Timestamp(future).floor("5min"),
        "open": 24_000.0, "high": 24_010.0, "low": 23_990.0,
        "close": 24_005.0, "volume": 1000.0,
    }])
    report = import_index_candles(db, df, "NIFTY", "5m", "test")
    assert count_rows(db) == 0
    assert report.rejection.counts["future_dated"] == 1


def test_weekend_bars_are_rejected(db):
    """2026-06-20 is a Saturday. The mock broker steps forward five minutes
    at a time with no notion of weekends, so its bars land on valid
    boundaries on days the market was shut."""
    report = import_index_candles(
        db, session_bars(date(2026, 6, 20)), "NIFTY", "5m", "mock")
    assert count_rows(db) == 0
    assert report.rejection.counts["outside_session"] == 12


def test_exchange_holiday_bars_are_rejected(db):
    """2026-01-26 is Republic Day — a Monday the market is shut. Weekday
    filtering alone lets these through."""
    report = import_index_candles(
        db, session_bars(date(2026, 1, 26)), "NIFTY", "5m", "test")
    assert count_rows(db) == 0
    assert report.rejection.counts["exchange_holiday"] == 12


def test_impossible_bars_are_rejected(db):
    """A high below the close is not a suspicious bar, it is not a bar."""
    df = session_bars(date(2026, 6, 16), count=4)
    df.loc[1, "high"] = df.loc[1, "low"] - 10      # high below low
    df.loc[2, "close"] = df.loc[2, "high"] + 50    # close above high
    df.loc[3, "open"] = -1.0                       # negative price

    report = import_index_candles(db, df, "NIFTY", "5m", "test")
    assert report.rejection.counts["impossible_ohlc"] == 3
    assert count_rows(db) == 1


def test_every_rejection_is_counted_not_just_logged(db):
    """The failure this accounting exists to catch: a source changes format,
    most rows are discarded, and the import still reports a plausible
    number with nothing anywhere saying what went missing."""
    good = session_bars(date(2026, 6, 16), count=5)
    weekend = session_bars(date(2026, 6, 20), count=4)
    report = import_index_candles(
        db, pd.concat([good, weekend]), "NIFTY", "5m", "test")

    assert report.rejection.rows_in == 9
    assert report.rejection.rows_out == 5
    assert report.rejection.rejected == 4
    assert sum(report.rejection.counts.values()) == 4


def test_a_mostly_rejected_import_says_so_in_words(db):
    """Counts are for machines. Somebody has to read this at 9am."""
    good = session_bars(date(2026, 6, 16), count=1)
    weekend = session_bars(date(2026, 6, 20), count=9)
    report = import_index_candles(
        db, pd.concat([good, weekend]), "NIFTY", "5m", "test")
    assert any("format" in w for w in report.rejection.warnings())


def test_empty_input_is_not_an_error(db):
    """The agent calls this every five minutes. Outside market hours there
    is legitimately nothing new."""
    report = import_index_candles(
        db, pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"]),
        "NIFTY", "5m", "test")
    assert report.write.written == 0
    assert count_rows(db) == 0


# ---- validation unit level --------------------------------------------

def test_impossible_mask_accepts_a_doji():
    """A bar where open, high, low and close are all equal is legitimate —
    it happens in thin instruments — and must not be swept up by the
    ordering checks."""
    df = pd.DataFrame([{
        "timestamp": pd.Timestamp("2026-06-16 04:00", tz="UTC"),
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 5.0,
    }])
    assert not impossible_mask(df).any()


def test_unknown_calendar_year_is_reported_not_guessed():
    """A year with no published holiday list must degrade to weekday-only
    and say so, rather than assert that year had no holidays.

    2019-06-17 is a Monday in a year the calendar does not carry. It has to
    be a past date: future-dating is a separate rejection that would mask
    the behaviour under test."""
    df = session_bars(date(2019, 6, 17))
    clean, report = clean_candles(df, "5m")
    assert len(clean) == 12
    assert 2019 in report.unverified_calendar_years
    assert any("2019" in w for w in report.warnings())
