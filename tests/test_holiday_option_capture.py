"""An exchange holiday must not enter the option archive as a trading day.

Audit finding M-5. The collector's gate is `market_hours.is_open`, which
knows about weekends and clock hours but not about holidays — so on Gandhi
Jayanti the collector polled NSE every sixty seconds all day. NSE answers a
holiday request with the *previous session's* chain: same strikes, same
prices, HTTP 200. Nothing in the payload looks wrong, so roughly 375
identical snapshots were filed against a day the market never opened, and
aggregated into bars whose high, low and close are all the closing price.

Option history is the one dataset here that cannot be rebuilt. A backtest
reading those bars prices trades against a chain that never moved.

The gate is the chain's own timestamp rather than the holiday list, and the
asymmetry is the whole design. Refusing a real session because a
mis-transcribed holiday said so destroys data that can never be recovered.
Refusing a replay costs nothing, because a replay carries no information
that is not already stored. So: refuse on evidence, warn on the calendar.
"""
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers.nse import parse_option_chain
from app.data.importer import import_option_snapshot
from app.data.quality import option_bars_on_non_sessions
from app.market_hours import IST
from app.models import OptionCandle, OptionContract


def chain(source_time: str | None = None) -> pd.DataFrame:
    df = pd.DataFrame({
        "strike": [24_000.0, 24_100.0],
        "call_ltp": [180.0, 120.0], "put_ltp": [90.0, 130.0],
        "call_oi": [1000.0, 2000.0], "put_oi": [1500.0, 900.0],
        "call_iv": [12.5, 12.1], "put_iv": [13.0, 12.8],
        "call_volume": [500.0, 400.0], "put_volume": [300.0, 200.0],
    })
    df.attrs["expiry"] = "02-Jul-2026"
    df.attrs["source_time"] = source_time
    return df


def ist(day: date, hour=11, minute=0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


# Muharram, Friday 26 June 2026: a weekday the exchange is shut, which is
# exactly the case the weekday-only gate lets through. Chosen because it is
# one of the two 2026 entries actually verified against the NSE circular, so
# these tests do not rest on a date that is still provisional — and because
# it is in the past, which the importer requires of any stored snapshot.
HOLIDAY = date(2026, 6, 26)
PREVIOUS_SESSION = date(2026, 6, 25)
EXPIRY = date(2026, 7, 2)


def stored_bars(db) -> int:
    return db.query(OptionCandle).count()


# ---- the regression ---------------------------------------------------

def test_a_replayed_chain_on_a_holiday_is_refused(db):
    """The exact shape: NSE serves Thursday's chain on Friday's holiday."""
    captured = ist(HOLIDAY, 11, 0)
    replay = chain(source_time=ist(PREVIOUS_SESSION, 15, 30).astimezone(UTC).isoformat())

    report = import_option_snapshot(
        db, replay, underlying="NIFTY", expiry=EXPIRY, spot=24_050.0,
        source="nse", captured_at=captured, timeframe="5m", lot_size=75)

    assert report.refused, "the replay was stored"
    assert "2026-06-25" in report.refused
    assert "did not trade" in report.refused
    assert stored_bars(db) == 0


def test_a_day_of_polling_a_holiday_stores_nothing(db):
    """The collector keeps polling — that is deliberate — so the archive has
    to hold the line every single time, not just the first."""
    replay = ist(PREVIOUS_SESSION, 15, 30).astimezone(UTC).isoformat()

    for minute in range(0, 60, 5):
        import_option_snapshot(
            db, chain(source_time=replay), underlying="NIFTY",
            expiry=EXPIRY, spot=24_050.0, source="nse",
            captured_at=ist(HOLIDAY, 10, minute), timeframe="5m", lot_size=75)

    assert stored_bars(db) == 0


def test_a_live_chain_on_a_real_session_is_stored(db):
    """The guard must not cost a real session. This is the failure direction
    that cannot be undone."""
    captured = ist(PREVIOUS_SESSION, 11, 0)
    live = chain(source_time=captured.astimezone(UTC).isoformat())

    report = import_option_snapshot(
        db, live, underlying="NIFTY", expiry=EXPIRY, spot=24_050.0,
        source="nse", captured_at=captured, timeframe="5m", lot_size=75)

    assert report.refused is None, report.refused
    assert stored_bars(db) > 0


def test_a_stale_chain_is_refused_even_on_a_trading_day(db):
    """Evidence, not the calendar. An unlisted closure or a source serving
    yesterday's data is caught by the same check."""
    captured = ist(PREVIOUS_SESSION, 11, 0)          # a normal Thursday
    stale = chain(source_time=ist(date(2026, 6, 24), 15, 30).astimezone(UTC).isoformat())

    report = import_option_snapshot(
        db, stale, underlying="NIFTY", expiry=EXPIRY, spot=24_050.0,
        source="nse", captured_at=captured, timeframe="5m", lot_size=75)

    assert report.refused
    assert stored_bars(db) == 0


# ---- when the source cannot corroborate --------------------------------

def test_no_timestamp_on_a_holiday_is_stored_but_flagged(db):
    """A calendar alone is not worth an unrecoverable deletion. The 2026
    list is still provisional, and a mis-transcribed date here would throw
    away a session that can never be re-collected."""
    report = import_option_snapshot(
        db, chain(source_time=None), underlying="NIFTY", expiry=EXPIRY,
        spot=24_050.0, source="mock", captured_at=ist(HOLIDAY, 11, 0),
        timeframe="5m", lot_size=75)

    assert report.refused is None
    assert stored_bars(db) > 0
    assert any("not a trading day" in w for w in report.warnings)
    assert any("suspect" in w for w in report.warnings)


def test_no_timestamp_on_a_trading_day_stores_quietly(db):
    report = import_option_snapshot(
        db, chain(source_time=None), underlying="NIFTY", expiry=EXPIRY,
        spot=24_050.0, source="mock", captured_at=ist(PREVIOUS_SESSION, 11, 0),
        timeframe="5m", lot_size=75)

    assert report.refused is None
    assert not any("not a trading day" in w for w in report.warnings)


def test_the_out_of_hours_warning_still_fires_on_a_session(db):
    """The behaviour that existed before, preserved."""
    report = import_option_snapshot(
        db, chain(source_time=ist(PREVIOUS_SESSION, 17, 0).astimezone(UTC).isoformat()),
        underlying="NIFTY", expiry=EXPIRY, spot=24_050.0, source="nse",
        captured_at=ist(PREVIOUS_SESSION, 17, 0), timeframe="5m", lot_size=75)

    assert any("outside market hours" in w for w in report.warnings)


# ---- the chain carries its own clock -----------------------------------

def test_the_parser_attaches_the_exchange_timestamp():
    payload = {"records": {
        "timestamp": "25-Jun-2026 15:30",
        "underlyingValue": 24_050.0,
        "expiryDates": ["02-Jul-2026"],
        "data": [{"strikePrice": 24_000, "expiryDate": "02-Jul-2026",
                  "CE": {"lastPrice": 180.0, "openInterest": 1000},
                  "PE": {"lastPrice": 90.0, "openInterest": 1500}}],
    }}
    parsed, _ = parse_option_chain(payload)
    stamped = datetime.fromisoformat(parsed.attrs["source_time"])
    assert stamped.astimezone(IST).date() == PREVIOUS_SESSION
    assert stamped.astimezone(IST).hour == 15


def test_a_payload_with_no_timestamp_parses_to_none():
    payload = {"records": {
        "underlyingValue": 24_050.0, "expiryDates": ["02-Jul-2026"],
        "data": [{"strikePrice": 24_000, "expiryDate": "02-Jul-2026",
                  "CE": {"lastPrice": 180.0}, "PE": {"lastPrice": 90.0}}],
    }}
    parsed, _ = parse_option_chain(payload)
    assert parsed.attrs["source_time"] is None


# ---- what is already in the archive ------------------------------------

def seed_bar(db, session_date: date) -> None:
    contract = db.query(OptionContract).first()
    if contract is None:
        seen = ist(PREVIOUS_SESSION, 11, 0).astimezone(UTC)
        contract = OptionContract(
            underlying="NIFTY", expiry_date=EXPIRY, strike=24_000.0,
            option_type="CE", lot_size=75, source="nse",
            first_seen=seen, last_seen=seen)
        db.add(contract)
        db.flush()
    db.add(OptionCandle(
        contract_id=contract.id, timeframe="5m",
        timestamp=ist(session_date, 11, 0).astimezone(UTC),
        open=180.0, high=180.0, low=180.0, close=180.0,
        bar_kind="snapshot", source="nse", session_date=session_date))
    db.commit()


def test_existing_holiday_bars_are_reported_as_an_error(db):
    """The importer stops new ones arriving. Rows captured before the guard
    existed are still there, and cannot be re-collected — so they have to be
    findable."""
    seed_bar(db, HOLIDAY)
    seed_bar(db, PREVIOUS_SESSION)

    findings = option_bars_on_non_sessions(db, "NIFTY")
    offending = [f for f in findings if f.check == "option_bars_on_non_sessions"]

    assert len(offending) == 1
    assert offending[0].severity == "error"
    assert offending[0].count == 1
    assert "2026-06-26" in offending[0].samples[0]
    assert "provisional" in offending[0].summary


def test_a_clean_archive_reports_nothing(db):
    seed_bar(db, PREVIOUS_SESSION)
    assert [f for f in option_bars_on_non_sessions(db, "NIFTY")
            if f.check == "option_bars_on_non_sessions"] == []


def test_a_year_with_no_calendar_is_reported_not_condemned(db):
    """`is_session` answers None for an unlisted year. Treating that as "not
    a session" would condemn a year of real data."""
    seed_bar(db, date(2019, 6, 10))

    findings = option_bars_on_non_sessions(db, "NIFTY")
    assert [f for f in findings if f.check == "option_bars_on_non_sessions"] == []
    unverified = [f for f in findings if f.check == "option_sessions_unverified"]
    assert unverified and 2019 in unverified[0].detail["years"]


def test_an_empty_archive_reports_nothing(db):
    assert option_bars_on_non_sessions(db, "NIFTY") == []
