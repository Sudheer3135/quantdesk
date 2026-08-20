"""Tests for open-interest change classification.

The old check flagged 229 changes on one day of real NSE data, essentially
all of them at at-the-money strikes on the eve of expiry with millions of
contracts traded behind them. It was measuring size and calling it
suspicion.

The replacement measures *consistency*: open interest moves only when
contracts are traded, so a move larger than the interval's volume is
impossible, and a move of any size matched by volume is just a busy market.

These tests pin down the five cases that matter, in both directions — that
real activity is not called corruption, and that corruption is still caught.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data.oi_classifier import (
    ANOMALY,
    HIGH_ACTIVITY,
    INSUFFICIENT_EVIDENCE,
    NORMAL,
    OIChange,
    classify,
)


def change(**kwargs) -> OIChange:
    """A liquid, mid-session, well-observed contract unless stated otherwise."""
    base = dict(
        strike=24_300.0, option_type="CE", timestamp="2026-08-17T04:50:00+00:00",
        oi=250_000.0, previous_oi=250_000.0, delta_oi=0.0,
        interval_volume=100_000.0, typical_move=500.0, observations=20,
        same_session=True, expiring=False, moneyness_pct=0.2,
    )
    base.update(kwargs)
    return OIChange(**base)


# ---- the five cases the redesign exists for ---------------------------

def test_high_volume_atm_oi_change_is_not_an_anomaly():
    """The case that produced almost all 229 false warnings: a real
    at-the-money OI shift with millions of contracts behind it."""
    verdict = classify(change(
        previous_oi=323_107.0, oi=257_025.0, delta_oi=-66_082.0,
        interval_volume=1_400_000.0, typical_move=800.0, moneyness_pct=0.1))

    assert verdict.classification == HIGH_ACTIVITY
    assert verdict.classification != ANOMALY
    assert "supported by trading" in verdict.reason


def test_huge_oi_change_with_negligible_volume_is_an_anomaly():
    """The signal the old check could not see. 66,000 contracts of open
    interest cannot appear on 40 contracts of trading."""
    verdict = classify(change(
        previous_oi=100_000.0, oi=166_000.0, delta_oi=66_000.0,
        interval_volume=40.0, typical_move=500.0))

    assert verdict.classification == ANOMALY
    assert "larger than the volume" in verdict.reason


def test_oi_moving_with_no_trading_at_all_is_an_anomaly():
    """Every change in open interest requires a trade. Zero volume and a
    non-zero change is a contradiction, not a quiet market."""
    verdict = classify(change(
        previous_oi=100_000.0, oi=105_000.0, delta_oi=5_000.0,
        interval_volume=0.0))

    assert verdict.classification == ANOMALY
    assert "no trading" in verdict.reason


def test_an_illiquid_far_otm_contract_yields_insufficient_evidence():
    """A far strike drifting by a few dozen contracts on no trading cannot
    be judged either way. Saying so is the honest answer; the old check
    called this noise a discontinuity.

    Note which reason wins: the move is below one lot, so it is beneath the
    resolution of the check regardless of how much history exists. The
    short history is reported separately, by the test below."""
    verdict = classify(change(
        strike=27_200.0, moneyness_pct=12.0, observations=3,
        previous_oi=1_200.0, oi=1_260.0, delta_oi=60.0, interval_volume=0.0,
        typical_move=0.0))

    assert verdict.classification == INSUFFICIENT_EVIDENCE
    assert "one lot" in verdict.reason


def test_expiry_day_atm_activity_is_legitimate_high_activity():
    """Positions close en masse on expiry day. That is the calendar, not a
    fault — and the reason must say so plainly enough that nobody
    investigates it twice."""
    verdict = classify(change(
        previous_oi=471_541.0, oi=0.0, delta_oi=-471_541.0,
        interval_volume=900_000.0, typical_move=1_000.0,
        expiring=True, moneyness_pct=0.05))

    assert verdict.classification == HIGH_ACTIVITY
    assert "expiry day" in verdict.reason
    assert "at the money" in verdict.reason


def test_genuinely_malformed_data_is_an_anomaly():
    """Cumulative volume cannot fall inside a session. When it does, the
    feed restated or reordered a bar."""
    verdict = classify(change(
        previous_oi=200_000.0, oi=205_000.0, delta_oi=5_000.0,
        interval_volume=-12_000.0))

    assert verdict.classification == ANOMALY
    assert "cannot happen" in verdict.reason


# ---- the remaining contradictions -------------------------------------

def test_negative_open_interest_is_an_anomaly():
    assert classify(change(oi=-5.0, previous_oi=100.0, delta_oi=-105.0)) \
        .classification == ANOMALY


def test_oi_vanishing_mid_session_without_expiry_is_an_anomaly():
    """Distinct from the expiry case above: same collapse, but the contract
    is not expiring and the volume does not explain it away."""
    verdict = classify(change(
        previous_oi=180_000.0, oi=0.0, delta_oi=-180_000.0,
        interval_volume=200_000.0, typical_move=900.0, expiring=False))

    assert verdict.classification == ANOMALY
    assert "zero mid-session" in verdict.reason


def test_a_volume_supported_collapse_on_expiry_is_not_flagged_as_a_reset():
    """The ordering that matters: the volume test runs before the
    zero-OI test, so a contract genuinely closed out by heavy trading is
    not reported as corrupt."""
    verdict = classify(change(
        previous_oi=50_000.0, oi=0.0, delta_oi=-50_000.0,
        interval_volume=120_000.0, typical_move=400.0, expiring=True))
    assert verdict.classification != ANOMALY


# ---- boundaries and non-findings --------------------------------------

def test_an_unchanged_contract_reports_nothing():
    assert classify(change(delta_oi=0.0)).classification is NORMAL


def test_an_ordinary_supported_move_reports_nothing():
    """Most bars must produce no finding at all, or the report is noise."""
    verdict = classify(change(
        previous_oi=250_000.0, oi=252_000.0, delta_oi=2_000.0,
        interval_volume=50_000.0, typical_move=1_500.0))
    assert verdict.classification is NORMAL


def test_a_session_boundary_is_not_comparable():
    """Overnight, open interest resets its daily change and cumulative
    volume restarts from zero. Nothing here applies across that break."""
    verdict = classify(change(
        same_session=False, previous_oi=300_000.0, oi=50_000.0,
        delta_oi=-250_000.0, interval_volume=-2_000_000.0))
    assert verdict.classification is NORMAL


def test_a_sub_lot_move_is_below_the_resolution_of_the_check():
    """Smaller than one NIFTY lot, in either direction, is not worth an
    argument — and calling it an anomaly on zero volume would fire
    constantly on far strikes."""
    verdict = classify(change(
        previous_oi=1_000.0, oi=1_040.0, delta_oi=40.0, interval_volume=0.0))
    assert verdict.classification == INSUFFICIENT_EVIDENCE
    assert "one lot" in verdict.reason


def test_missing_volume_is_unknown_rather_than_suspicious():
    """A source that publishes no volume makes the check inapplicable. That
    is not evidence of a problem with the open interest."""
    verdict = classify(change(
        previous_oi=100_000.0, oi=140_000.0, delta_oi=40_000.0,
        interval_volume=None))
    assert verdict.classification == INSUFFICIENT_EVIDENCE
    assert "no interval volume" in verdict.reason


def test_small_snapshot_skew_is_tolerated():
    """OI and volume are sampled a moment apart and NSE's OI figure lags
    slightly, so a move a few percent above the interval volume is that
    skew rather than corruption. Without the tolerance this would fire on
    ordinary bars."""
    verdict = classify(change(
        previous_oi=100_000.0, oi=110_500.0, delta_oi=10_500.0,
        interval_volume=10_000.0, typical_move=8_000.0))
    assert verdict.classification != ANOMALY


def test_a_move_a_third_above_volume_is_not_excused_as_skew():
    """The tolerance must not be so generous that it swallows real
    contradictions."""
    verdict = classify(change(
        previous_oi=100_000.0, oi=120_000.0, delta_oi=20_000.0,
        interval_volume=10_000.0))
    assert verdict.classification == ANOMALY


@pytest.mark.parametrize("observations", [1, 2, 4])
def test_a_young_contract_cannot_be_judged_on_size(observations):
    """Fewer than five bars is not a history. Anything said about whether a
    move is unusual would be invented."""
    verdict = classify(change(
        observations=observations, previous_oi=100_000.0, oi=150_000.0,
        delta_oi=50_000.0, interval_volume=60_000.0))
    assert verdict.classification == INSUFFICIENT_EVIDENCE


def test_a_young_contract_is_still_checked_for_contradictions():
    """Evidence gates *size* judgements, not physics. A negative reading is
    impossible however new the contract is."""
    verdict = classify(change(observations=1, oi=-10.0, previous_oi=5.0,
                              delta_oi=-15.0))
    assert verdict.classification == ANOMALY


def test_every_verdict_carries_a_reason():
    """A classification with no explanation cannot be acted on, and gets
    ignored instead of investigated."""
    for kwargs in ({}, {"delta_oi": 5_000.0, "interval_volume": 0.0},
                   {"observations": 2}, {"same_session": False}):
        assert classify(change(**kwargs)).reason
