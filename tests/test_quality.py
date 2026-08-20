"""Tests for the data-quality diagnostics.

Two things are being checked, and the second matters as much as the first:
that each diagnostic fires on the fault it is named for, and that it stays
quiet otherwise. A check that reports every exchange holiday as missing data
gets ignored within a month — and then so does the one real outage.

Several tests insert rows directly rather than through the importer. That is
deliberate: the importer would reject them, and the whole point of these
diagnostics is to catch what is already in the table — rows that predate a
guard, or arrived through a bulk load that bypassed it.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import quality
from app.data.importer import import_index_candles, import_option_snapshot
from app.models import CandleRecord, OptionCandle, OptionContract
from test_importer import session_bars
from test_option_data import MOMENT, chain

IST_OFFSET = timedelta(hours=5, minutes=30)


def full_session(db, day: date, bars: int = 75, source: str = "free", **kwargs):
    import_index_candles(db, session_bars(day, count=bars, **kwargs),
                         "NIFTY", "5m", source)


def raw_candle(db, day: date, minute_offset: int = 0, **overrides):
    """Insert a row the importer would have rejected."""
    ts = datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC) \
        + timedelta(minutes=minute_offset)
    row = {
        "symbol": "NIFTY", "timeframe": "5m", "timestamp": ts,
        "open": 24_000.0, "high": 24_010.0, "low": 23_990.0, "close": 24_005.0,
        "volume": 1000.0, "source": "free", "session_date": day,
        "volume_is_synthetic": False, "revision": 0,
    }
    row.update(overrides)
    db.add(CandleRecord(**row))
    db.commit()


def findings(report, check):
    return [f for f in report["findings"] if f["check"] == check]


# ---- missing candles --------------------------------------------------

def test_a_complete_archive_is_clean(db):
    """The baseline. If this ever fails, every other finding is noise."""
    for day in (16, 17, 18, 19):
        full_session(db, date(2026, 6, day))

    report = quality.report(db, include_options=False)
    assert report["errors"] == 0
    assert report["index"]["verdict"] in {"clean", "usable with caveats"}
    assert report["index"]["backtest_eligible"] is True


def test_a_missing_trading_day_is_an_error(db):
    """2026-06-18 is a Thursday the market was open. Nothing captured is a
    real outage, not a holiday."""
    for day in (16, 17, 19):
        full_session(db, date(2026, 6, day))

    found = findings(quality.report(db, include_options=False), "missing_sessions")
    assert found and found[0]["severity"] == "error"
    assert "2026-06-18" in found[0]["samples"]


def test_weekends_are_not_reported_as_missing(db):
    """Between Friday and Monday there are two absent days and no fault."""
    full_session(db, date(2026, 6, 19))      # Friday
    full_session(db, date(2026, 6, 22))      # Monday

    assert not findings(quality.report(db, include_options=False),
                        "missing_sessions")


def test_an_exchange_holiday_is_not_reported_as_missing(db):
    """2026-08-15 is Independence Day — a Saturday that year, so the more
    telling case is Republic Day, 2026-01-26, a Monday the market is shut.
    Without the calendar this is indistinguishable from an outage."""
    full_session(db, date(2026, 1, 23))      # Friday
    full_session(db, date(2026, 1, 27))      # Tuesday

    found = findings(quality.report(db, include_options=False), "missing_sessions")
    absent = found[0]["samples"] if found else []
    assert "2026-01-26" not in absent


def test_a_short_session_is_reported_separately_from_an_absent_one(db):
    """A session that is present but missing bars is a feed that dropped
    them mid-flight — a different problem from a day never captured, and the
    one you can actually act on."""
    full_session(db, date(2026, 6, 16), bars=75)
    full_session(db, date(2026, 6, 17), bars=40)     # short
    full_session(db, date(2026, 6, 18), bars=75)

    found = findings(quality.report(db, include_options=False), "incomplete_sessions")
    assert found and found[0]["severity"] == "warning"
    assert found[0]["samples"][0]["session"] == "2026-06-17"
    assert found[0]["detail"]["expected_bars_per_session"] == 75


def test_the_most_recent_session_is_not_flagged_as_short(db):
    """It is legitimately partial while it is still being traded. Flagging
    it every afternoon is how a check becomes background noise."""
    full_session(db, date(2026, 6, 16), bars=75)
    full_session(db, date(2026, 6, 17), bars=20)     # today, still running

    assert not findings(quality.report(db, include_options=False),
                        "incomplete_sessions")


# ---- impossible prices ------------------------------------------------

def test_a_high_below_the_low_is_an_error(db):
    full_session(db, date(2026, 6, 16))
    raw_candle(db, date(2026, 6, 17), high=23_000.0, low=24_500.0)

    found = findings(quality.report(db, include_options=False), "impossible_prices")
    assert found and found[0]["severity"] == "error"
    assert quality.report(db, include_options=False)["index"]["verdict"] == "unusable"


def test_a_close_outside_the_range_is_an_error(db):
    full_session(db, date(2026, 6, 16))
    raw_candle(db, date(2026, 6, 17), close=99_999.0)
    assert findings(quality.report(db, include_options=False), "impossible_prices")


def test_a_non_positive_price_is_an_error(db):
    full_session(db, date(2026, 6, 16))
    raw_candle(db, date(2026, 6, 17), low=0.0)
    assert findings(quality.report(db, include_options=False), "impossible_prices")


def test_duplicates_are_reported_none_when_the_constraint_holds(db):
    """The unique constraint makes a duplicate impossible to create through
    the schema, so there is no positive case to construct here. The check
    stays because rows predating the constraint, or arriving through a bulk
    load that bypasses the importer, would otherwise be invisible — and a
    duplicated bar is a bar traded twice."""
    full_session(db, date(2026, 6, 16))
    assert quality.duplicate_candles(db, "NIFTY", "5m") == []


# ---- price jumps ------------------------------------------------------

def test_a_large_intraday_jump_is_a_warning_not_an_error(db):
    """NIFTY does gap. Calling a real move corrupt would be worse than
    missing a bad tick, so this never escalates to an error."""
    full_session(db, date(2026, 6, 16), bars=10)
    raw_candle(db, date(2026, 6, 16), minute_offset=50,
               open=30_000.0, high=30_100.0, low=29_900.0, close=30_000.0)

    found = findings(quality.report(db, include_options=False), "price_jumps")
    assert found and found[0]["severity"] == "warning"


def test_an_overnight_gap_is_not_reported(db):
    """The gap between Friday's close and Monday's open is not a data
    fault, and reporting it would fire on most Mondays."""
    full_session(db, date(2026, 6, 16), bars=5, start_price=24_000.0)
    full_session(db, date(2026, 6, 17), bars=5, start_price=26_000.0)

    assert not findings(quality.report(db, include_options=False), "price_jumps")


# ---- provenance -------------------------------------------------------

def test_synthetic_volume_is_surfaced(db):
    """The consequence is otherwise invisible: the volume check silently
    contributes nothing, the weights renormalise around it, and the backtest
    looks entirely healthy while running one input short."""
    full_session(db, date(2026, 6, 16), real_volume=False)

    found = findings(quality.report(db, include_options=False), "synthetic_volume")
    assert found and found[0]["severity"] == "warning"
    assert "placeholder" in found[0]["summary"]


def test_mock_rows_in_the_archive_are_an_error(db):
    """A random walk mixed into real history makes every statistic
    meaningless, and nothing about the numbers would look wrong."""
    full_session(db, date(2026, 6, 16), source="mock")

    found = findings(quality.report(db, include_options=False), "mock_data_present")
    assert found and found[0]["severity"] == "error"


def test_the_source_mix_is_always_reported(db):
    full_session(db, date(2026, 6, 16), source="free")
    full_session(db, date(2026, 6, 17), source="kite")

    found = findings(quality.report(db, include_options=False), "source_mix")
    assert found
    assert set(found[0]["detail"]) == {"free", "kite"}


# ---- options ----------------------------------------------------------

def test_an_empty_option_archive_explains_why(db):
    """The most important finding on day one, and it must not read as a
    fault: there is no free source to backfill from."""
    found = findings(quality.report(db), "option_history_empty")
    assert found and found[0]["severity"] == "info"
    assert "snapshot, not a tape" in found[0]["summary"]


def test_single_sample_option_bars_are_flagged(db):
    """A bar built from one poll has a high and low equal to its close. The
    range is fiction, and anything measuring intrabar movement over it will
    understate the market."""
    import_option_snapshot(db, chain(), underlying="NIFTY", expiry="18-Jun-2026",
                           spot=24_450.0, source="free", captured_at=MOMENT)

    found = findings(quality.report(db), "single_sample_bars")
    assert found and found[0]["severity"] == "warning"


def test_a_hole_in_the_strike_ladder_is_flagged(db):
    """Strikes are listed at a fixed interval. A gap means the capture
    missed rows, and a strike selection could silently land on a different
    contract than intended."""
    import_option_snapshot(
        db, chain(strikes=(24_400, 24_500, 24_550)), underlying="NIFTY",
        expiry="18-Jun-2026", spot=24_450.0, source="free", captured_at=MOMENT)

    found = findings(quality.report(db), "missing_option_strikes")
    assert found
    assert found[0]["samples"][0]["strike"] == 24_450.0


def test_a_complete_ladder_is_not_flagged(db):
    import_option_snapshot(
        db, chain(strikes=(24_400, 24_450, 24_500, 24_550)), underlying="NIFTY",
        expiry="18-Jun-2026", spot=24_450.0, source="free", captured_at=MOMENT)
    assert not findings(quality.report(db), "missing_option_strikes")


def test_an_absurd_stored_iv_is_an_error(db):
    """The unit trap: NSE publishes a percentage, the pricing model takes a
    fraction. 13.5 stored where 0.135 belongs raises nothing and prices
    every option about a hundred times too high."""
    import_option_snapshot(db, chain(), underlying="NIFTY", expiry="18-Jun-2026",
                           spot=24_450.0, source="free", captured_at=MOMENT)
    bar = db.scalars(select(OptionCandle)).first()
    bar.iv = 13.5                      # the percentage, stored raw
    db.commit()

    found = findings(quality.report(db), "abnormal_iv")
    assert found and found[0]["severity"] == "error"


def test_negative_open_interest_is_an_anomaly(db):
    """Impossible whatever the volume, and caught even on a contract with
    almost no history behind it."""
    import_option_snapshot(db, chain(), underlying="NIFTY", expiry="18-Jun-2026",
                           spot=24_450.0, source="free", captured_at=MOMENT)
    import_option_snapshot(db, chain(), underlying="NIFTY", expiry="18-Jun-2026",
                           spot=24_450.0, source="free",
                           captured_at=MOMENT + timedelta(minutes=6))
    bar = db.scalars(select(OptionCandle).order_by(OptionCandle.timestamp.desc())).first()
    bar.open_interest = -5.0
    db.commit()

    found = findings(quality.report(db), "oi_anomaly")
    assert found, "negative open interest must be reported"
    assert found[0]["severity"] == "warning"
    assert any("negative" in s["reason"] for s in found[0]["samples"])


def test_oi_collapsing_without_the_volume_to_explain_it_is_an_anomaly(db):
    """Open interest accumulates and decays; it does not vanish at noon on
    almost no trading. Volume is grown realistically across the snapshots so
    the interval volume is a real number rather than a fixture artifact —
    it simply is not large enough to account for the collapse."""
    for step in range(6):
        import_option_snapshot(
            db, chain(volume=400_000.0 + step * 500), underlying="NIFTY",
            expiry="18-Jun-2026", spot=24_450.0, source="free",
            captured_at=MOMENT + timedelta(minutes=6 * step))

    contract = db.scalars(select(OptionContract)).first()
    bars = db.scalars(
        select(OptionCandle)
        .where(OptionCandle.contract_id == contract.id)
        .order_by(OptionCandle.timestamp)).all()
    bars[3].open_interest = 0.0
    db.commit()

    found = findings(quality.report(db), "oi_anomaly")
    assert found, "a collapse unsupported by volume must be reported"
    assert found[0]["severity"] == "warning"


def test_expiry_day_oi_collapse_backed_by_volume_is_not_an_anomaly(db):
    """OI genuinely goes to nothing when a contract expires, and that
    unwinding is accompanied by heavy trading. Flagging it would be
    flagging the calendar."""
    expiry_day = MOMENT.date()
    for step in range(6):
        import_option_snapshot(
            db, chain(volume=400_000.0 + step * 900_000), underlying="NIFTY",
            expiry=expiry_day.strftime("%d-%b-%Y"), spot=24_450.0,
            source="free", captured_at=MOMENT + timedelta(minutes=6 * step))

    contract = db.scalars(select(OptionContract)).first()
    bars = db.scalars(
        select(OptionCandle)
        .where(OptionCandle.contract_id == contract.id)
        .order_by(OptionCandle.timestamp)).all()
    bars[3].open_interest = 0.0
    db.commit()

    anomalies = findings(quality.report(db), "oi_anomaly")
    reasons = " ".join(s["reason"] for f in anomalies for s in f["samples"])
    assert "zero mid-session" not in reasons


def test_high_volume_oi_moves_are_reported_as_activity_not_problems(db):
    """The 229 false warnings this redesign exists to remove. Large OI
    moves matched by large volume are the market working."""
    for step in range(6):
        import_option_snapshot(
            db, chain(oi=900_000.0 - step * 60_000, volume=400_000.0 + step * 900_000),
            underlying="NIFTY", expiry="18-Jun-2026", spot=24_450.0,
            source="free", captured_at=MOMENT + timedelta(minutes=6 * step))

    report = quality.report(db)
    assert not findings(report, "oi_anomaly"), "supported moves must not be anomalies"


def test_the_oi_diagnostic_never_marks_the_dataset_unusable(db):
    """Explicit requirement: this check is informational. A busy expiry day
    must never gate a backtest."""
    for step in range(6):
        import_option_snapshot(
            db, chain(oi=900_000.0 - step * 200_000, volume=400_000.0 + step * 100),
            underlying="NIFTY", expiry="18-Jun-2026", spot=24_450.0,
            source="free", captured_at=MOMENT + timedelta(minutes=6 * step))

    report = quality.report(db)
    oi_findings = [f for f in report["findings"] if f["check"].startswith("oi_")]
    assert oi_findings, "test premise: expected some OI findings"
    assert all(f["severity"] != "error" for f in oi_findings)


# ---- the report -------------------------------------------------------

def test_findings_are_ordered_worst_first(db):
    full_session(db, date(2026, 6, 16), source="mock", real_volume=False)
    raw_candle(db, date(2026, 6, 17), high=1.0, low=99_999.0)

    report = quality.report(db, include_options=False)
    severities = [f["severity"] for f in report["findings"]]
    rank = {"error": 0, "warning": 1, "info": 2}
    assert severities == sorted(severities, key=lambda s: rank[s])


def test_the_verdict_reflects_the_worst_finding(db):
    full_session(db, date(2026, 6, 16), real_volume=False)
    assert quality.report(db, include_options=False)["index"]["verdict"] == "usable with caveats"

    raw_candle(db, date(2026, 6, 17), high=1.0, low=99_999.0)
    assert quality.report(db, include_options=False)["index"]["verdict"] == "unusable"


def test_an_empty_database_does_not_crash_any_diagnostic(db):
    report = quality.report(db)
    assert report["errors"] == 0
    assert report["verdict"] != "unusable"


@pytest.mark.parametrize("timeframe,expected", [
    ("5m", 75), ("15m", 25), ("1m", 375), ("1h", 6)])
def test_expected_bars_per_session(timeframe, expected):
    """09:15 to 15:30 is 375 minutes."""
    assert quality.expected_bars(timeframe) == expected
