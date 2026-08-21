"""The verdict on screen is the verdict now, not the verdict at 10:00.

A risk decision is made when a signal is published, and the trade journal
moves underneath it. Approve a plan at 10:00, open the position at 10:01, and
by 10:02 the open-position cap has been reached — but the agent does not tick
again until 10:05, and `signal:latest` has a fifteen-minute TTL, so a browser
that reconnects replays the 10:00 approval as though it were current.

Two decisions, then, and they answer different questions. The stored one is
what the desk decided and must never be rewritten — that is the audit trail.
The live one is what it would decide about the same levels right now, and
that is the one you act on.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics.signal_engine import Signal
from app.models import TradeRecord
from app.risk import live as risk_live


def a_buy():
    return Signal(
        symbol="NIFTY", timeframe="5m", timestamp=datetime.now(UTC).isoformat(),
        action="BUY", confidence=0.62, price=24_200.0,
        entry=24_200.0, stop_loss=24_190.0, target=24_225.0,
        risk_reward=2.5, checks=[], context={},
    )


def open_position(db, minutes_ago=0):
    db.add(TradeRecord(
        symbol="NIFTY", side="BUY", quantity=75, entry=24_200.0,
        stop_loss=24_190.0, status="open",
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    ))
    db.commit()


# ---- the scenario from the brief --------------------------------------

def test_approved_at_ten_then_blocked_by_a_position_opened_at_one_past(db):
    """10:00 approved · 10:01 position opened · 10:02 current risk blocked ·
    the 10:00 decision still reads approved."""
    published = {"action": "BUY", "entry": 24_200.0,
                 "stop_loss": 24_190.0, "target": 24_225.0}
    at_ten = risk_live.decide(db, a_buy())
    published["risk"] = at_ten
    assert at_ten["state"] == "approved"

    open_position(db)                       # 10:01

    now = risk_live.current(db, published)  # 10:02
    assert now["state"] == "blocked"
    assert any("Already holding" in r for r in now["reasons"])

    # The stored decision is untouched — a governance record that rewrites
    # itself records nothing.
    assert published["risk"] is at_ten
    assert published["risk"]["state"] == "approved"
    assert published["risk"]["approved"] is True


def test_the_two_verdicts_carry_different_timestamps(db):
    published = {"action": "BUY", "entry": 24_200.0,
                 "stop_loss": 24_190.0, "target": 24_225.0,
                 "risk": risk_live.decide(db, a_buy())}
    open_position(db)
    now = risk_live.current(db, published)

    assert published["risk"]["evaluated_at"] < now["evaluated_at"]


def test_current_agrees_when_nothing_has_changed(db):
    """Recomputing must not invent a disagreement."""
    published = {"action": "BUY", "entry": 24_200.0,
                 "stop_loss": 24_190.0, "target": 24_225.0,
                 "risk": risk_live.decide(db, a_buy())}

    now = risk_live.current(db, published)
    assert now["state"] == published["risk"]["state"] == "approved"
    assert now["quantity"] == published["risk"]["quantity"]


def test_current_uses_the_signals_own_levels(db):
    """Not a fresh signal — the same plan, judged against a newer journal."""
    published = {"action": "BUY", "entry": 24_200.0,
                 "stop_loss": 24_190.0, "target": 24_225.0}
    now = risk_live.current(db, published)

    assert now["evaluated"] is True
    assert now["risk_per_unit"] == pytest.approx(10.0)


def test_current_on_a_hold_reports_no_trade(db):
    now = risk_live.current(db, {"action": "HOLD", "entry": None,
                                 "stop_loss": None, "target": None})
    assert now["state"] == "not-applicable"


def test_current_tolerates_having_no_signal_at_all(db):
    assert risk_live.current(db, None) is None
    assert risk_live.current(db, "not a payload") is None


def test_current_reflects_a_live_kill_switch(db, monkeypatch):
    """The two P0s meeting: the switch is read now, and "now" is what the
    current verdict means."""
    from app import killswitch

    published = {"action": "BUY", "entry": 24_200.0,
                 "stop_loss": 24_190.0, "target": 24_225.0,
                 "risk": risk_live.decide(db, a_buy())}
    assert published["risk"]["state"] == "approved"

    monkeypatch.setenv(killswitch.ENV_VAR, "true")

    now = risk_live.current(db, published)
    assert now["state"] == "blocked"
    assert any("Kill switch" in r for r in now["reasons"])
    assert published["risk"]["state"] == "approved", "history was rewritten"


# ---- one implementation, two questions ---------------------------------

def test_current_and_decide_agree_on_the_same_inputs(db):
    """`current` must not become a second implementation of the rules."""
    open_position(db)
    sig = a_buy()

    from_signal = risk_live.decide(db, sig)
    from_payload = risk_live.current(db, {
        "action": sig.action, "entry": sig.entry,
        "stop_loss": sig.stop_loss, "target": sig.target})

    assert {k: v for k, v in from_signal.items() if k != "evaluated_at"} == \
           {k: v for k, v in from_payload.items() if k != "evaluated_at"}
