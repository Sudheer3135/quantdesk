"""What the signal journal hands the dashboard.

The feed on the trading terminal shows one row per stored signal, and the
row has to answer four questions the action alone cannot: which way did the
higher timeframe point, was this the moment, what kind of market was it, and
would the desk have been allowed to take it.

All four already live on the row. The endpoint was discarding them on the
way out, so the feed could only ever show an action and a confidence — which
is exactly the single fused number that splitting the output into layers
existed to get away from.

The other half of what these pin down is that the change is **additive**. A
response that renamed or dropped a field would break every existing caller
silently, and the dashboard is not the only thing that reads this.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import signals as signals_api  # noqa: E402
from app.db import get_db  # noqa: E402
from app.models import SignalRecord  # noqa: E402

# Every key the endpoint returned before the dashboard redesign. Named
# explicitly rather than derived, so a rename has to be a deliberate edit
# here and cannot ride along with a refactor.
ORIGINAL_FIELDS = {
    "id", "created_at", "symbol", "action", "confidence",
    "price", "entry", "stop_loss", "target",
}


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def store(db, **over):
    fields = {
        "symbol": "NIFTY", "timeframe": "5m", "action": "BUY",
        "confidence": 0.61, "price": 24_200.0, "entry": 24_200.0,
        "stop_loss": 24_150.0, "target": 24_320.0, "checks": [], "context": {},
        "created_at": datetime.now(UTC) - timedelta(minutes=5),
        "bias": "BULLISH", "entry_state": "WAIT_PULLBACK",
        "plan": {"entry": {"state": "WAIT_PULLBACK",
                           "regime_day": "TREND_UP", "regime_hour": "RANGE"}},
        "risk": {"state": "blocked",
                 "reasons": ["Daily trade cap reached (2)."]},
    }
    row = SignalRecord(**{**fields, **over})
    db.add(row)
    db.commit()
    return row


def test_the_feed_gets_bias_entry_state_regime_and_risk(client, db):
    store(db)
    row = client.get("/signals/history?limit=5").json()[0]

    assert row["bias"] == "BULLISH"
    assert row["entry_state"] == "WAIT_PULLBACK"
    assert row["regime_day"] == "TREND_UP"
    assert row["regime_hour"] == "RANGE"
    assert row["risk_state"] == "blocked"


def test_every_field_the_endpoint_used_to_return_is_still_returned(client, db):
    """Additive means additive. Another caller must see no change at all."""
    store(db)
    row = client.get("/signals/history").json()[0]
    assert ORIGINAL_FIELDS <= set(row), ORIGINAL_FIELDS - set(row)


def test_the_verdict_travels_without_its_reasons(client, db):
    """A feed row needs approved-or-not at a glance. The reasons are long,
    belong to the detail view, and would make the journal unreadable."""
    store(db)
    row = client.get("/signals/history").json()[0]
    assert row["risk_state"] == "blocked"
    assert "reasons" not in row
    assert "Daily trade cap" not in str(row)


def test_a_row_with_no_plan_reports_null_rather_than_guessing(client, db):
    """Rows predate the plan columns. The feed shows a blank for those, and
    a reconstructed regime would be a fabricated audit record."""
    store(db, bias=None, entry_state=None, plan=None, risk=None)
    row = client.get("/signals/history").json()[0]

    assert row["bias"] is None
    assert row["entry_state"] is None
    assert row["regime_day"] is None
    assert row["regime_hour"] is None
    assert row["risk_state"] is None


def test_a_plan_without_an_entry_block_does_not_crash_the_feed(client, db):
    """The plan is JSON the desk wrote, not a schema the API validates. A
    shape it did not expect must cost a field, never the request."""
    store(db, plan={"bias": {"label": "BULLISH"}})
    row = client.get("/signals/history").json()[0]
    assert row["regime_day"] is None
    assert row["bias"] == "BULLISH"


def test_the_regime_is_read_from_the_stored_plan_not_recomputed(client, db):
    """The row must say what the desk actually saw at the time. A regime
    re-derived now would describe today's market, not that bar's."""
    store(db, plan={"entry": {"regime_day": "VOLATILE_CHOP",
                              "regime_hour": "SQUEEZE"}})
    row = client.get("/signals/history").json()[0]
    assert row["regime_day"] == "VOLATILE_CHOP"
    assert row["regime_hour"] == "SQUEEZE"


def test_the_feed_is_newest_first(client, db):
    older = store(db, created_at=datetime.now(UTC) - timedelta(hours=2))
    newer = store(db, created_at=datetime.now(UTC) - timedelta(minutes=1))
    ids = [r["id"] for r in client.get("/signals/history").json()]
    assert ids.index(newer.id) < ids.index(older.id)
