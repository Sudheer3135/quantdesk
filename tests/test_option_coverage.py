"""Tests for option-snapshot coverage.

This diagnostic exists because of a specific incident. On 17-Aug-2026 the
collector stopped at 12:50 IST and never resumed. Index candles backfilled
from Yahoo as normal, every other check stayed green, and a two-hour-forty
hole in an archive that cannot be rebuilt was found only by reading the
table by hand.

So the tests are written around the ways collection actually fails — a late
start, an early stop, a hole in the middle, a dead session — and around the
ways a naive version of this check would cry wolf: holidays, weekends, and
the last bucket of an otherwise perfect day.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import quality
from app.data.importer import import_option_snapshot
from app.data.option_coverage import (
    EARLY_STOP,
    LATE_START,
    MID_SESSION,
    WHOLE_SESSION,
    assess_session,
    assessable_sessions,
    expected_bucket_starts,
    expected_polls_for,
    find_gaps,
)
from app.market_hours import IST
from test_option_data import EXPIRY, chain

# A Monday, and a real trading session.
SESSION = date(2026, 8, 17)
BAR_MINUTES = 5
POLL_SECONDS = 60
POLLS_PER_BUCKET = 5


def bucket(hh: int, mm: int, day: date = SESSION) -> datetime:
    """A bucket start given in IST, returned in UTC as stored."""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def full_day(day: date = SESSION, polls: int = POLLS_PER_BUCKET) -> dict:
    return dict.fromkeys(expected_bucket_starts(day, BAR_MINUTES), polls)


# ---- the shape of a session -------------------------------------------

def test_a_session_holds_75_buckets_and_375_one_minute_snapshots():
    """09:15 to 15:30 is 375 minutes: 75 five-minute buckets, and at 60s
    polling one snapshot per minute."""
    buckets = expected_bucket_starts(SESSION, BAR_MINUTES)
    assert len(buckets) == 75
    assert buckets[0] == bucket(9, 15)
    assert buckets[-1] == bucket(15, 25)
    assert expected_polls_for(SESSION, BAR_MINUTES, POLL_SECONDS) == 375


def test_the_bucket_starting_at_the_close_is_not_expected():
    """Only a poll fired at exactly 15:30:00 could land there, so counting
    it would report a one-bucket gap at the end of nearly every otherwise
    flawless session — the classic way a check becomes background noise."""
    assert bucket(15, 30) not in expected_bucket_starts(SESSION, BAR_MINUTES)


# ---- 1. complete session ----------------------------------------------

def test_a_complete_session_reports_full_coverage_and_no_gaps():
    coverage = assess_session(SESSION, full_day(), BAR_MINUTES, POLL_SECONDS)

    assert coverage.coverage_pct == 100.0
    assert coverage.observed_polls == 375
    assert coverage.gaps == []
    assert coverage.severity() == "info"
    assert coverage.missing_minutes == 0


def test_a_complete_session_is_not_flagged_for_one_missing_poll():
    """A poll landing a second late slips into the next bucket. A perfect
    day routinely lands a fraction under 100%, and calling that an incident
    would make the check unreadable."""
    polls = full_day()
    polls[bucket(11, 0)] = POLLS_PER_BUCKET - 1
    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)

    assert coverage.coverage_pct > 99.0
    assert coverage.severity() == "info"
    assert coverage.under_sampled_buckets == 1


# ---- 2. short isolated gap --------------------------------------------

def test_a_short_isolated_gap_is_a_warning_not_an_error():
    """Fifteen minutes missing out of a session is real and worth seeing,
    and nowhere near enough to make the day unusable."""
    polls = full_day()
    for hh, mm in ((11, 0), (11, 5), (11, 10)):
        del polls[bucket(hh, mm)]

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)

    assert len(coverage.gaps) == 1
    gap = coverage.gaps[0]
    assert gap.kind == MID_SESSION
    assert gap.minutes == 15
    assert gap.missing_buckets == 3
    assert gap.start_ist.endswith("11:00")
    assert gap.end_ist.endswith("11:15")
    assert coverage.severity() == "warning"


def test_two_separate_gaps_are_reported_separately():
    """Merging them would understate how broken collection was."""
    polls = full_day()
    del polls[bucket(10, 0)]
    del polls[bucket(13, 0)]

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)
    assert len(coverage.gaps) == 2
    assert all(g.kind == MID_SESSION for g in coverage.gaps)


# ---- 3. long contiguous outage ----------------------------------------

def test_a_long_contiguous_outage_is_one_gap_and_an_error():
    """The 17-Aug incident, reproduced: collection to 12:50 and nothing
    after. One gap, not thirty-two, because the run is contiguous."""
    polls = {b: POLLS_PER_BUCKET for b in expected_bucket_starts(SESSION, BAR_MINUTES)
             if b < bucket(12, 55)}

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)

    assert len(coverage.gaps) == 1
    gap = coverage.gaps[0]
    assert gap.kind == EARLY_STOP
    assert gap.minutes == 155              # 12:55 to 15:30
    assert gap.start_ist.endswith("12:55")
    assert gap.end_ist.endswith("15:30")
    assert coverage.coverage_pct < 90.0
    assert coverage.severity() == "error"


# ---- 4. holiday --------------------------------------------------------

def test_an_exchange_holiday_is_never_assessed():
    """2026-08-15 is Independence Day. A holiday with no snapshots is not
    an outage, and reporting it as one is how this check would get
    ignored."""
    sessions = assessable_sessions(date(2026, 8, 13), date(2026, 8, 18))
    assert date(2026, 8, 15) not in sessions
    assert date(2026, 8, 17) in sessions


def test_a_verified_holiday_inside_the_window_is_skipped():
    """26-Jun-2026 is Muharram — verified against the NSE circular."""
    sessions = assessable_sessions(date(2026, 6, 24), date(2026, 6, 30))
    assert date(2026, 6, 26) not in sessions
    assert date(2026, 6, 25) in sessions


# ---- 5. weekend --------------------------------------------------------

def test_weekends_are_never_assessed():
    sessions = assessable_sessions(date(2026, 8, 14), date(2026, 8, 17))
    assert date(2026, 8, 15) not in sessions      # Saturday
    assert date(2026, 8, 16) not in sessions      # Sunday
    assert sessions == [date(2026, 8, 14), date(2026, 8, 17)]


# ---- 6. partial-session collector startup ------------------------------

def test_a_late_collector_start_is_named_as_such():
    """A gap touching the open is a process that started late, which points
    somewhere different from a hole in the middle of the day."""
    polls = {b: POLLS_PER_BUCKET for b in expected_bucket_starts(SESSION, BAR_MINUTES)
             if b >= bucket(9, 40)}

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)

    assert len(coverage.gaps) == 1
    gap = coverage.gaps[0]
    assert gap.kind == LATE_START
    assert gap.start_ist.endswith("09:15")
    assert gap.end_ist.endswith("09:40")
    assert gap.minutes == 25
    assert coverage.first_ist.endswith("09:40")


# ---- 7. collector shutdown before market close -------------------------

def test_an_early_collector_shutdown_is_named_as_such():
    polls = {b: POLLS_PER_BUCKET for b in expected_bucket_starts(SESSION, BAR_MINUTES)
             if b < bucket(15, 0)}

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)

    assert coverage.gaps[-1].kind == EARLY_STOP
    assert coverage.gaps[-1].end_ist.endswith("15:30")
    assert coverage.last_ist.endswith("15:00")


def test_a_session_with_no_data_at_all_is_a_whole_session_outage():
    """Distinct from a partial one: nothing was captured, and that is a
    different conversation from 'we missed twenty minutes'."""
    coverage = assess_session(SESSION, {}, BAR_MINUTES, POLL_SECONDS)

    assert len(coverage.gaps) == 1
    assert coverage.gaps[0].kind == WHOLE_SESSION
    assert coverage.coverage_pct == 0.0
    assert coverage.severity() == "error"
    assert coverage.first_ist is None


# ---- severity is configurable -----------------------------------------

def test_the_error_threshold_is_configurable():
    """The 90% default is a placeholder, not a derived figure, so the
    caller must be able to argue with it."""
    polls = {b: POLLS_PER_BUCKET for b in expected_bucket_starts(SESSION, BAR_MINUTES)
             if b < bucket(14, 30)}
    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)

    assert 80.0 < coverage.coverage_pct < 90.0
    assert coverage.severity(min_backtest_pct=90.0) == "error"
    assert coverage.severity(min_backtest_pct=80.0) == "warning"


def test_an_empty_session_is_an_error_at_any_threshold():
    """No amount of lowering the bar makes zero data usable."""
    coverage = assess_session(SESSION, {}, BAR_MINUTES, POLL_SECONDS)
    assert coverage.severity(min_backtest_pct=0.0) == "error"


# ---- gap mechanics -----------------------------------------------------

def test_buckets_outside_the_session_do_not_count_towards_coverage():
    """A forced out-of-hours snapshot is a separate matter; it must not
    inflate a session it does not belong to."""
    polls = full_day()
    polls[bucket(16, 30)] = 5          # after the close
    polls[bucket(8, 30)] = 5           # before the open

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)
    assert coverage.observed_buckets == 75
    assert coverage.coverage_pct == 100.0


def test_extra_samples_in_a_bucket_do_not_inflate_coverage():
    """Twenty polls into one bucket does not make up for a missing one."""
    polls = full_day()
    del polls[bucket(11, 0)]
    polls[bucket(11, 5)] = 40

    coverage = assess_session(SESSION, polls, BAR_MINUTES, POLL_SECONDS)
    assert coverage.coverage_pct < 100.0


def test_find_gaps_on_an_empty_expectation_is_not_an_error():
    assert find_gaps([], set()) == []


def test_assessable_sessions_needs_both_bounds():
    """No snapshots means no basis for judging any session. Returning a
    list of 'missing' days would be inventing an outage."""
    assert assessable_sessions(None, None) == []
    assert assessable_sessions(SESSION, None) == []


# ---- through the diagnostic -------------------------------------------

def store(db, at, samples=1):
    """Persist one snapshot at `at` (UTC)."""
    return import_option_snapshot(
        db, chain(), underlying="NIFTY", expiry=EXPIRY, spot=24_450.0,
        source="free", captured_at=at, timeframe="5m")


def coverage_findings(db, checks):
    report = quality.report(db)
    return [f for f in report["findings"] if f["check"] in checks]


def test_the_diagnostic_reports_nothing_measurable_on_an_empty_archive(db):
    """It must not claim the collector was down. There is simply nothing to
    measure, and those are different statements."""
    found = coverage_findings(db, {"option_snapshot_coverage"})
    assert found and found[0]["severity"] == "info"
    assert "not a report that the collector was down" in found[0]["summary"]


def test_the_diagnostic_detects_a_real_early_stop(db):
    """End to end against stored rows: snapshots through 09:55 IST and
    nothing after, on a session that ran to 15:30."""
    for minute in range(0, 45, 5):
        store(db, bucket(9, 15) + timedelta(minutes=minute))

    found = coverage_findings(db, {"option_coverage_critical"})
    assert found, "a session ending at 10:00 must be reported"
    assert found[0]["severity"] == "error"

    session = found[0]["samples"][0]
    assert session["session_date"] == SESSION.isoformat()
    assert session["coverage_pct"] < 90.0
    assert any(g["kind"] == EARLY_STOP for g in session["gaps"])


def test_the_diagnostic_never_assesses_days_before_collection_began(db):
    """Only the window bounded by real snapshots is judged."""
    store(db, bucket(9, 15))

    found = coverage_findings(db, {"option_snapshot_coverage"})
    detail = found[0]["detail"]
    assert detail["assessed_from"] == SESSION.isoformat()
    assert detail["assessed_to"] == SESSION.isoformat()
    assert "assuming it was would invent an outage" in detail["note"]


def test_the_diagnostic_reports_coverage_per_expiry(db):
    """A ladder can roll mid-session and leave one series far thinner than
    the session total suggests."""
    store(db, bucket(9, 15))
    import_option_snapshot(
        db, chain(), underlying="NIFTY", expiry="25-Jun-2026", spot=24_450.0,
        source="free", captured_at=bucket(9, 20), timeframe="5m")

    found = coverage_findings(db, {"option_snapshot_coverage"})
    by_expiry = found[0]["detail"]["by_expiry"]
    assert len(by_expiry) == 2
    assert all(row["expected_polls"] > 0 for row in by_expiry)


@pytest.mark.parametrize("poll_seconds,expected", [(60, 375), (30, 750), (300, 75)])
def test_expected_polls_track_the_configured_interval(poll_seconds, expected):
    """Change the collector's cadence and the yardstick moves with it —
    otherwise a faster collector would look permanently incomplete."""
    assert expected_polls_for(SESSION, BAR_MINUTES, poll_seconds) == expected
