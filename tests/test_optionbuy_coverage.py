"""The gate that refuses rather than running a fictional backtest.

Option history cannot be backfilled. NSE publishes a live snapshot rather
than a tape, so a session the collector missed is gone permanently — no
vendor sells it back and no amount of waiting recovers it. A run over a
window with holes therefore has exactly two honest outcomes: refuse, or
model the holes and label every one of those fills.

What it must never do is fill the hole with something that looks like data.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.optionbuy import chain as chain_module
from app.optionbuy import coverage as coverage_module
from app.optionbuy.coverage import EvidenceGate, gate
from app.optionbuy.pricing import MODELLED_ONLY, OBSERVED_ONLY, PREFER_OBSERVED
from optionbuy_fixtures import (
    candles,
    seed_index_rows,
    seed_option_rows,
    sessions,
)

DAYS = sessions(date(2025, 6, 2), 6)
EXPIRY = date(2025, 6, 17)
STRIKES = [23_900.0, 23_950.0, 24_000.0, 24_050.0, 24_100.0]


def seed(db, option_days=None, **kwargs):
    seed_index_rows(db, candles(DAYS))
    if option_days:
        seed_option_rows(db, option_days, EXPIRY, strikes=STRIKES, **kwargs)


def run_gate(db, policy=PREFER_OBSERVED, **kwargs):
    store = (chain_module.empty_store() if policy == MODELLED_ONLY
             else chain_module.load(db))
    return gate(db, policy=policy, store=store, **kwargs)


# ---- the index still has to be there ----------------------------------

def test_missing_index_candles_refuse_before_anything_else(db):
    """A modelled run still needs the bars the signals come from, and
    reporting that as an option problem would send the fix the wrong way."""
    report = run_gate(db, policy=MODELLED_ONLY)
    assert not report.ok
    assert "index archive" in report.reason
    assert report.to_dict()["error"] == "insufficient option coverage"
    assert "import/index" in report.to_dict()["fix"]


def test_too_few_index_sessions_refuse(db):
    seed_index_rows(db, candles(sessions(date(2025, 6, 2), 2)))
    report = run_gate(db, policy=MODELLED_ONLY, min_sessions=5)
    assert not report.ok
    assert "session(s) stored" in report.reason


# ---- modelled runs are allowed, and labelled ---------------------------

def test_a_modelled_run_passes_without_reading_the_archive(db):
    seed(db)
    report = run_gate(db, policy=MODELLED_ONLY)

    assert report.ok
    assert report.options["consulted"] is False
    assert "labelled MODELLED" in report.options["note"]
    assert any("study of the model" in w for w in report.warnings)


# ---- observed_only is strict ------------------------------------------

def test_observed_only_refuses_when_the_archive_is_empty(db):
    seed(db)
    report = run_gate(db, policy=OBSERVED_ONLY)
    assert not report.ok
    assert "no bars at all" in report.reason
    assert "cannot be backfilled" in report.to_dict()["fix"]


def test_observed_only_refuses_a_window_with_one_missing_session(db):
    """The hole is the point. A gate that passed on the average would let a
    strategy trade a day nobody collected."""
    seed(db, option_days=DAYS[:5])              # the sixth session is missing
    report = run_gate(db, policy=OBSERVED_ONLY)

    assert not report.ok
    assert "1 of 6 sessions" in report.reason
    assert DAYS[5].isoformat() in report.reason
    per_session = {s["session"]: s for s in report.options["per_session"]}
    assert per_session[DAYS[5].isoformat()]["coverage_pct"] == 0.0
    assert per_session[DAYS[0].isoformat()]["covered"] is True


def test_a_partly_collected_session_fails_the_floor(db):
    """The 17-Aug outage shape: collection ran, then stopped at midday.
    Every other diagnostic stayed green."""
    seed(db, option_days=DAYS[:5])
    seed_option_rows(db, DAYS[5:], EXPIRY, strikes=STRIKES, bars=30)

    report = run_gate(db, policy=OBSERVED_ONLY)
    assert not report.ok
    per_session = {s["session"]: s for s in report.options["per_session"]}
    partial = per_session[DAYS[5].isoformat()]
    assert 0 < partial["coverage_pct"] < 90
    assert partial["covered"] is False


def test_observed_only_passes_a_fully_collected_window(db):
    seed(db, option_days=DAYS)
    report = run_gate(db, policy=OBSERVED_ONLY)

    assert report.ok, report.reason
    assert report.options["sessions_missing"] == 0
    assert report.options["sessions_covered"] == 6
    assert report.options["archive"]["rows"] > 0
    assert report.options["archive"]["hash"]


def test_observed_only_still_needs_a_minimum_sample(db):
    """Six perfectly collected sessions is not evidence of anything, and a
    gate that only checked completeness would say it was."""
    short = sessions(date(2025, 6, 2), 6)
    seed_index_rows(db, candles(short))
    seed_option_rows(db, short, EXPIRY, strikes=STRIKES)

    report = run_gate(db, policy=OBSERVED_ONLY, min_sessions=20)
    assert not report.ok
    assert "at least 20" in report.reason


# ---- prefer_observed warns instead of refusing ------------------------

def test_prefer_observed_passes_a_partial_window_with_a_named_warning(db):
    seed(db, option_days=DAYS[:4])
    report = run_gate(db, policy=PREFER_OBSERVED)

    assert report.ok
    assert report.options["sessions_missing"] == 2
    assert any("labelled MODELLED" in w for w in report.warnings)
    assert any("assumptions, not fills" in w for w in report.warnings)


def test_prefer_observed_refuses_when_there_is_nothing_to_prefer(db):
    """Asking to prefer the archive over a window it holds nothing for is a
    request for a modelled run made by accident. Say so rather than
    returning one silently."""
    seed(db)
    report = run_gate(db, policy=PREFER_OBSERVED)
    assert not report.ok
    assert "no bars at all" in report.reason


def test_a_thin_observed_sample_is_warned_about_even_when_it_passes(db):
    """Six sessions of index history and two of option history. The run is
    allowed; what it can conclude from the OBSERVED half is not."""
    seed(db, option_days=DAYS[:2])
    report = run_gate(db, policy=PREFER_OBSERVED, min_sessions=5)

    assert report.ok
    assert report.options["sessions_covered"] == 2
    assert any("too small to conclude" in w for w in report.warnings)


# ---- sessions the collector never ran are graded, not skipped ---------

def test_every_trading_session_in_the_window_is_graded(db):
    """Grading only the sessions with data grades the collector on the days
    it ran — the measurement that made a two-hour-forty outage invisible."""
    seed(db, option_days=DAYS[:2])
    report = run_gate(db, policy=PREFER_OBSERVED)
    assert report.options["sessions_in_window"] == 6
    assert len(report.options["per_session"]) == 6


def test_weekends_are_not_counted_as_missing_sessions(db):
    """2-6 June 2025 is Mon-Fri; the window below spans a weekend."""
    span = sessions(date(2025, 6, 5), 3)         # Thu, Fri, Mon
    seed_index_rows(db, candles(span))
    seed_option_rows(db, span, EXPIRY, strikes=STRIKES)

    report = run_gate(db, policy=OBSERVED_ONLY, min_sessions=3)
    assert report.ok, report.reason
    assert report.options["sessions_in_window"] == 3


# ---- the second gate: what the fills turned out to be -----------------

def test_the_evidence_gate_passes_when_nothing_was_required():
    assert EvidenceGate(required_pct=0.0, achieved_pct=0.0, trades=4).ok


def test_the_evidence_gate_refuses_a_mostly_modelled_result():
    """Coverage is about sessions; this is about the trades actually taken.
    A window can clear the floor while every trade lands in the hole."""
    checked = EvidenceGate(required_pct=80.0, achieved_pct=25.0, trades=8)
    assert not checked.ok

    refusal = checked.refusal()
    assert refusal["error"] == "insufficient observed pricing"
    assert "25.0%" in refusal["reason"]
    assert "describes the model rather than the market" in refusal["reason"]
    assert "min_observed_pct" in refusal["fix"]


def test_the_evidence_gate_passes_when_the_share_is_met():
    assert EvidenceGate(required_pct=80.0, achieved_pct=80.0, trades=8).ok


# ---- configuration ------------------------------------------------------

def test_an_unknown_policy_is_rejected(db):
    with pytest.raises(ValueError, match="unknown pricing policy"):
        gate(db, policy="whatever", store=chain_module.empty_store())


def test_a_policy_that_reads_the_archive_needs_a_store(db):
    seed(db)
    with pytest.raises(ValueError, match="chain store is required"):
        gate(db, policy=OBSERVED_ONLY, store=None)


def test_the_floor_is_inherited_rather_than_restated():
    """One definition of "enough coverage" across the platform."""
    from app.data import option_coverage as platform

    assert (coverage_module.DEFAULT_MIN_SESSION_COVERAGE_PCT
            == platform.DEFAULT_MIN_BACKTEST_COVERAGE_PCT)
