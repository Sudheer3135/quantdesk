"""Index and option readiness are separate questions.

Merging them was actively misleading. On 17-Aug-2026 the option archive was
22% covered and the index archive was complete and clean, and the single
verdict said "unusable" — which was true of the options and false of the
candles, with nothing in the response to tell them apart.

The two datasets are collected by different mechanisms and fail in
different ways: index candles backfill from Yahoo, so a gap is recoverable;
option snapshots cannot be rebuilt at any price. They are fit for different
purposes and they need different answers.

The four cases below are the full matrix, because the failure being guarded
against is directional — options dragging the index down is the one that
already happened, and the reverse would be just as wrong.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import quality
from app.data.importer import import_index_candles, import_option_snapshot
from app.data.option_coverage import expected_bucket_starts
from app.market_hours import IST
from test_importer import session_bars
from test_option_data import EXPIRY, chain

# Real trading sessions: 26-Jun is Muharram, so it is deliberately absent.
SESSIONS = [date(2026, 6, d) for d in (16, 17, 18, 19, 22, 23, 24, 25)]
BARS_PER_SESSION = 75


def complete_index(db, days=SESSIONS):
    for day in days:
        import_index_candles(db, session_bars(day, count=BARS_PER_SESSION),
                             "NIFTY", "5m", "test")


def incomplete_index(db, days=SESSIONS):
    """Every session about half length — thin enough to fail the coverage
    floor, but with no structurally broken bar, so it produces warnings
    rather than errors."""
    for day in days:
        import_index_candles(db, session_bars(day, count=30), "NIFTY", "5m", "test")


def _store_option_buckets(db, day, buckets, samples=5):
    for start in buckets:
        for poll in range(samples):
            import_option_snapshot(
                db, chain(), underlying="NIFTY", expiry=EXPIRY, spot=24_450.0,
                source="free", captured_at=start + timedelta(minutes=poll),
                timeframe="5m")


def complete_options(db, day=SESSIONS[-1]):
    """A fully covered session: every bucket, every poll."""
    _store_option_buckets(db, day, expected_bucket_starts(day, 5))


def incomplete_options(db, day=SESSIONS[-1]):
    """The 17-Aug shape: collection stops a third of the way through."""
    buckets = expected_bucket_starts(day, 5)[:20]
    _store_option_buckets(db, day, buckets)


# ---- the four cases ---------------------------------------------------

def test_complete_index_and_incomplete_options(db):
    """The case that prompted this. Poor option coverage must not make a
    complete index archive look unusable."""
    complete_index(db)
    incomplete_options(db)

    report = quality.report(db)

    assert report["index"]["backtest_eligible"] is True
    assert report["index"]["verdict"] != "unusable"
    assert report["index"]["coverage"] == 100.0
    assert report["index"]["errors"] == 0

    assert report["options"]["backtest_eligible"] is False
    assert report["options"]["verdict"] == "unusable"
    assert report["options"]["coverage"] < 90.0

    # And the top level hides neither.
    assert "index" in report["verdict"] and "options" in report["verdict"]
    assert report["backtest_eligible"] == {"index": True, "options": False}


def test_incomplete_index_and_complete_options(db):
    """The mirror image. A thin index archive must not be rescued by good
    option coverage either."""
    incomplete_index(db)
    complete_options(db)

    report = quality.report(db)

    assert report["index"]["backtest_eligible"] is False
    assert report["index"]["coverage"] < 90.0

    assert report["options"]["backtest_eligible"] is True
    assert report["options"]["coverage"] >= 90.0
    assert report["options"]["errors"] == 0


def test_both_complete(db):
    complete_index(db)
    complete_options(db)

    report = quality.report(db)

    assert report["index"]["backtest_eligible"] is True
    assert report["options"]["backtest_eligible"] is True
    assert report["backtest_eligible"] == {"index": True, "options": True}
    assert report["errors"] == 0


def test_both_incomplete(db):
    incomplete_index(db)
    incomplete_options(db)

    report = quality.report(db)

    assert report["index"]["backtest_eligible"] is False
    assert report["options"]["backtest_eligible"] is False
    assert report["backtest_eligible"] == {"index": False, "options": False}


# ---- the shape of the response ----------------------------------------

@pytest.mark.parametrize("domain", ["index", "options"])
def test_each_domain_reports_the_required_fields(db, domain):
    complete_index(db)
    complete_options(db)

    block = quality.report(db)[domain]
    for key in ("verdict", "coverage", "errors", "warnings", "backtest_eligible"):
        assert key in block, f"{domain} is missing {key}"
    assert isinstance(block["backtest_eligible"], bool)
    assert 0.0 <= block["coverage"] <= 100.0


def test_findings_are_attributed_to_exactly_one_domain(db):
    """A finding belonging to both, or to neither, would make the per-domain
    error counts wrong in a way nothing else would reveal."""
    complete_index(db)
    incomplete_options(db)

    report = quality.report(db)
    total = len(report["findings"])
    split = len(report["index"]["findings"]) + len(report["options"]["findings"])
    assert split == total

    assert all(f["domain"] in {"index", "options"} for f in report["findings"])


def test_option_findings_never_appear_under_index(db):
    """The mechanism of the original bug: an option error counted against
    the index domain would make it unusable again."""
    complete_index(db)
    incomplete_options(db)

    report = quality.report(db)
    index_checks = {f["check"] for f in report["index"]["findings"]}
    assert not any(c.startswith("option_") or c.startswith("oi_") for c in index_checks)


def test_an_option_error_does_not_raise_the_index_error_count(db):
    complete_index(db)
    incomplete_options(db)

    report = quality.report(db)
    assert report["options"]["errors"] >= 1
    assert report["index"]["errors"] == 0
    # The overall total still tells the truth about both.
    assert report["errors"] == report["index"]["errors"] + report["options"]["errors"]


# ---- thresholds stay configurable -------------------------------------

def test_the_option_threshold_is_still_configurable(db):
    """Kept from the coverage work: 90% is a placeholder, not a derived
    figure, so it must remain arguable."""
    complete_index(db)
    incomplete_options(db)

    strict = quality.report(db, option_min_backtest_pct=90.0)
    lenient = quality.report(db, option_min_backtest_pct=1.0)

    assert strict["options"]["backtest_eligible"] is False
    assert lenient["options"]["min_coverage_pct"] == 1.0


def test_the_index_threshold_is_configurable(db):
    incomplete_index(db)

    strict = quality.report(db, include_options=False, index_min_backtest_pct=90.0)
    lenient = quality.report(db, include_options=False, index_min_backtest_pct=10.0)

    assert strict["index"]["backtest_eligible"] is False
    assert lenient["index"]["backtest_eligible"] is True


def test_coverage_measures_completeness_not_quantity(db):
    """Worth stating explicitly, because the field name invites the wrong
    reading. Two perfect sessions are 100% covered — coverage asks "is what
    you have whole?", not "do you have enough?".

    So `backtest_eligible` here is True on two days of data, and that is
    correct at this layer. Whether a *requested window* can be served is a
    different question, enforced by `repository.check_coverage`, which is
    what the backtest endpoint actually calls and which refuses a window
    with too few sessions."""
    complete_index(db, days=SESSIONS[:2])

    report = quality.report(db, include_options=False)
    assert report["index"]["errors"] == 0
    assert report["index"]["coverage"] == 100.0
    assert report["index"]["backtest_eligible"] is True
    assert report["index"]["expected_bars"] == 2 * BARS_PER_SESSION


def test_options_with_no_data_at_all_are_not_eligible(db):
    """Zero option snapshots must not read as 'no errors, therefore fine'."""
    complete_index(db)

    report = quality.report(db)
    assert report["options"]["coverage"] == 0.0
    assert report["options"]["backtest_eligible"] is False


def test_excluding_options_omits_the_block_rather_than_faking_it(db):
    complete_index(db)

    report = quality.report(db, include_options=False)
    assert "options" not in report
    assert report["backtest_eligible"]["options"] is False
    assert report["verdict"].startswith("index ")


def test_index_coverage_counts_a_missing_session_against_it(db):
    """A trading day absent from the middle of the range is lost coverage,
    not a smaller denominator. Shrinking the denominator instead would let
    an archive with holes report as complete."""
    present = [d for d in SESSIONS if d != date(2026, 6, 18)]
    complete_index(db, days=present)

    block = quality.report(db, include_options=False)["index"]

    # 18-Jun is a Thursday and a real session, so it still counts as expected.
    assert block["expected_bars"] == len(SESSIONS) * BARS_PER_SESSION
    assert block["observed_bars"] == len(present) * BARS_PER_SESSION
    assert block["coverage"] < 100.0
    # And a wholly absent trading day is an error, so the index is not
    # eligible regardless of how the percentage lands.
    assert block["errors"] >= 1
    assert block["backtest_eligible"] is False


def test_index_coverage_ignores_weekends_and_holidays(db):
    """26-Jun-2026 is Muharram and sits inside the seeded range. Counting it
    as an expected session would cap coverage below 100% forever."""
    complete_index(db)

    block = quality.report(db, include_options=False)["index"]
    assert block["coverage"] == 100.0
    assert block["expected_bars"] == len(SESSIONS) * BARS_PER_SESSION


def test_extra_bars_in_one_session_cannot_offset_a_short_one(db):
    """Yahoo returns a 15:30 bar on top of the 75 buckets. Letting a session
    exceed 100% would silently mask a genuinely short day elsewhere."""
    import_index_candles(db, session_bars(SESSIONS[0], count=76), "NIFTY", "5m", "test")
    import_index_candles(db, session_bars(SESSIONS[1], count=20), "NIFTY", "5m", "test")

    block = quality.report(db, include_options=False)["index"]
    assert block["coverage"] < 100.0


def bucket_at(hh, mm, day):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)
