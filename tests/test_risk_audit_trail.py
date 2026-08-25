"""What risk decision did QuantDesk make for this signal?

`SignalRecord` describes itself as "what makes the system auditable after
the fact", and stored no risk decision at all — so the one question a
governance record exists to answer could not be answered from the database.
You could see that a BUY was published at 10:00; you could not see whether
the desk had approved it, refused it, or never asked.

The decision now travels with the row, in its own column. Not inside
`context`: that holds the market reading the signal engine produced, and the
dashboard reads it as such.
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics.signal_engine import Signal
from app.api import signals as signals_api
from app.api.signals import Analysis
from app.config import get_settings
from app.db import get_db
from app.models import SignalRecord, TradeRecord
from app.workers import agent
from test_risk_on_live_path import SessionFactory


def a_buy(entry=24_200.0, stop=24_190.0, target=24_225.0):
    return Signal(
        symbol="NIFTY", timeframe="5m", timestamp=datetime.now(UTC).isoformat(),
        action="BUY", confidence=0.62, price=entry,
        entry=entry, stop_loss=stop, target=target,
        risk_reward=round((target - entry) / (entry - stop), 2),
        checks=[], context={"trend": "up"},
    )


@pytest.fixture
def ticked(db, monkeypatch):
    """Run one agent tick against the test database."""
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish", lambda *a, **k: True)

    def run(signal):
        monkeypatch.setattr(agent, "build_analysis",
                        lambda *a, **k: Analysis(signal=signal, plan=None))
        agent.tick(force=True)
        return db.query(SignalRecord).order_by(SignalRecord.id.desc()).first()

    return run


# ---- the agent's route -------------------------------------------------

def test_the_stored_signal_carries_its_decision(ticked):
    record = ticked(a_buy())

    assert record.risk is not None, "no risk decision stored"
    assert record.risk["state"] in {"approved", "blocked"}
    assert record.risk["evaluated"] is True


def test_the_record_answers_every_question_the_brief_asks(ticked):
    """approved/blocked · reasons · quantity · risk amount · when · state."""
    risk = ticked(a_buy()).risk

    assert isinstance(risk["approved"], bool)
    assert risk["reasons"], "a decision with no reasons explains nothing"
    assert "quantity" in risk and "lots" in risk
    assert "rupees_at_risk" in risk
    assert risk["potential"]["rupees_at_risk"] >= 0
    assert datetime.fromisoformat(risk["evaluated_at"]).tzinfo is not None
    assert set(risk["day_state"]) == {
        "trading_day", "trades_taken", "realised_pnl",
        "consecutive_losses", "open_positions"}


def test_a_blocked_signal_records_why_it_was_blocked(db, ticked):
    db.add(TradeRecord(symbol="NIFTY", side="BUY", quantity=75, entry=24_200.0,
                       stop_loss=24_190.0, status="open",
                       created_at=datetime.now(UTC)))
    db.commit()

    risk = ticked(a_buy()).risk

    assert risk["state"] == "blocked"
    assert any("Already holding" in r for r in risk["reasons"])
    assert risk["day_state"]["open_positions"] == 1


def test_a_hold_records_that_no_trade_was_proposed(ticked):
    record = ticked(Signal(
        symbol="NIFTY", timeframe="5m", timestamp=datetime.now(UTC).isoformat(),
        action="HOLD", confidence=0.2, price=24_200.0, checks=[], context={}))

    assert record.risk["state"] == "not-applicable"
    assert record.risk["evaluated"] is False


def test_the_stored_decision_matches_the_published_one(db, monkeypatch):
    """One evaluation, not two taken a moment apart — otherwise the audit
    trail records a decision the desk never actually showed."""
    sent = {}
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish",
                        lambda channel, blob, **kw: sent.update(json.loads(blob)))
    monkeypatch.setattr(agent, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_buy(), plan=None))

    agent.tick(force=True)

    stored = db.query(SignalRecord).order_by(SignalRecord.id.desc()).first()
    assert stored.risk == sent["risk"]


# ---- the endpoint's route ----------------------------------------------

def test_persisting_through_the_endpoint_records_the_decision(db, monkeypatch):
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_buy(), plan=None))
    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db

    body = TestClient(app).get("/signals/live?persist=true").json()

    stored = db.get(SignalRecord, body["id"])
    assert stored.risk == body["risk"]


def test_context_is_not_used_as_a_dumping_ground(ticked):
    """The decision lives in its own column. `context` is the market reading
    and must stay that."""
    record = ticked(a_buy())
    assert "risk" not in record.context
    assert record.context == {"trend": "up"}


# ---- rows written before the column existed ----------------------------

def test_a_row_with_no_decision_reads_as_unknown_not_as_approval(db):
    """The migration adds a nullable column and backfills nothing. Inventing
    a plausible decision for historical rows would put fiction into the audit
    trail; NULL says "not recorded", which is true."""
    legacy = SignalRecord(symbol="NIFTY", timeframe="5m", action="BUY",
                          confidence=0.6, price=24_200.0, entry=24_200.0,
                          stop_loss=24_190.0, target=24_225.0)
    db.add(legacy)
    db.commit()

    assert db.get(SignalRecord, legacy.id).risk is None
