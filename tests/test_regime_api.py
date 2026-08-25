"""The regime endpoints, and the paths the dashboard actually reads.

Covers the three ways a regime reaches a screen — the backfill that builds
the history, the split that explains the old strategy's results, and the
live reading the socket and its polling fallback both serve.

One thing here is a deliberate design assertion rather than a behaviour
check: `test_the_live_regime_comes_from_the_stored_table` pins the dashboard
to the same table the research split is built on. A second classification
inside the web process would be free to disagree with it, and the desk would
show RANGE while the table deciding which conditions are tradeable said
TREND_UP, with nothing reporting the contradiction.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import data as data_api
from app.api import signals as signals_api
from app.api import stream as stream_api
from app.data import regime_store
from app.data.importer import import_index_candles
from app.db import get_db
from app.market_hours import IST
from app.models import SignalRecord

BARS_PER_SESSION = 75


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(data_api.router)
    app.include_router(signals_api.router)
    app.include_router(stream_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def archive(db, n=300, seed=1):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 6.0, n)
    close = 24_000 + np.cumsum(steps)
    day = datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    stamps = []
    for i in range(n):
        stamps.append((day + timedelta(minutes=5 * (i % BARS_PER_SESSION)))
                      .astimezone(UTC))
        if (i + 1) % BARS_PER_SESSION == 0:
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
    frame = pd.DataFrame({
        "timestamp": stamps, "open": close - steps,
        "high": np.maximum(close, close - steps) + 10,
        "low": np.minimum(close, close - steps) - 10,
        "close": close, "volume": [1000.0] * n})
    import_index_candles(db, frame, "NIFTY", "5m", source="test")
    return frame


# ---- the backfill ------------------------------------------------------

def test_the_backfill_needs_a_key(client, db, monkeypatch):
    """It writes. Reading a regime is open; building the table is not."""
    monkeypatch.setenv("API_KEY", "secret")
    from app.config import get_settings
    get_settings.cache_clear()

    assert client.post("/data/regimes/backfill").status_code == 401


def test_the_backfill_classifies_the_archive(client, db):
    frame = archive(db)
    body = client.post("/data/regimes/backfill").json()

    assert body["classified"] == len(frame)
    assert body["inserted"] == len(frame)
    assert sum(body["day_labels"].values()) == len(frame)


def test_rebuild_clears_before_reclassifying(client, db):
    frame = archive(db)
    client.post("/data/regimes/backfill")
    body = client.post("/data/regimes/backfill?rebuild=true").json()

    assert body["cleared"] == len(frame)
    assert body["inserted"] == len(frame)
    assert body["updated"] == 0


def test_coverage_reports_the_engine_version(client, db):
    archive(db)
    client.post("/data/regimes/backfill")
    body = client.get("/data/regimes").json()

    assert body["rows"] > 0
    assert body["mixed_versions"] is False
    assert body["current_engine_version"] == body["engine_versions"].popitem()[0]


def test_coverage_before_any_backfill_points_at_it(client):
    body = client.get("/data/regimes").json()

    assert body["rows"] == 0
    assert "backfill" in body["note"]


# ---- the live reading --------------------------------------------------

def test_the_live_regime_comes_from_the_stored_table(client, db):
    """One source of truth. The dashboard and the research split must never
    be able to disagree about what condition the market is in."""
    archive(db)
    client.post("/data/regimes/backfill")

    served = client.get("/market/regime").json()
    stored = regime_store.latest(db, "NIFTY", "5m")

    assert served["day"]["label"] == stored["day"]["label"]
    assert served["hour"]["label"] == stored["hour"]["label"]
    assert served["timestamp"] == stored["timestamp"]


def test_the_live_regime_carries_its_reasoning(client, db):
    archive(db)
    client.post("/data/regimes/backfill")
    body = client.get("/market/regime").json()

    assert body["day"]["reasons"]
    assert body["hour"]["reasons"]
    assert 0.0 <= body["day"]["confidence"] <= 1.0
    assert body["engine_version"]


def test_an_unclassified_desk_returns_the_same_shape(client):
    """A caller that has to branch on which keys came back will eventually
    forget to, and the branch it forgets is the empty one."""
    body = client.get("/market/regime").json()

    assert body["day"] is None
    assert body["hour"] is None
    assert "backfill" in body["note"]
    assert set(body) >= {"timestamp", "session_date", "engine_version",
                         "day", "hour"}


# ---- the split ---------------------------------------------------------

def test_the_regime_split_endpoint_returns_buckets(client, db):
    frame = archive(db)
    for index in (100, 150, 200):
        stamp = pd.Timestamp(frame["timestamp"].iloc[index]).to_pydatetime()
        price = float(frame["close"].iloc[index])
        db.add(SignalRecord(
            symbol="NIFTY", timeframe="5m", action="BUY", confidence=0.6,
            price=price, entry=price, stop_loss=price - 20, target=price + 40,
            checks=[], context={"trend": "bullish"},
            created_at=(stamp + timedelta(seconds=30)).astimezone(UTC)))
    db.commit()
    client.post("/data/regimes/backfill")

    body = client.get("/signals/outcomes/by-regime").json()

    assert body["selection"]["selected"] == 3
    assert body["matched"] == 3
    assert body["unmatched"] == 0
    assert sum(b["n"] for b in body["by_day_regime"]) == 3
    assert all("interpretation" in b for b in body["by_day_regime"])


def test_the_split_refuses_an_unknown_symbol(db):
    """Through the real app, because the 422 comes from a handler registered
    there — a bare router would raise instead, and the test would be
    asserting something production does not do."""
    from app.main import app as real_app
    real_app.dependency_overrides[get_db] = lambda: db
    try:
        response = TestClient(real_app).get("/signals/outcomes/by-regime?symbol=DOGE")
    finally:
        real_app.dependency_overrides.clear()

    assert response.status_code == 422


def test_the_headline_study_still_reports_the_engine_trend_label(client, db):
    """The old `by_regime` field held the signal engine's `context["trend"]`,
    which was never a market regime. It is now named for what it is, so two
    different things do not sit under one name in the same report."""
    archive(db)
    body = client.get("/signals/outcomes").json()

    assert "by_trend_label" in body
    assert "by_regime" not in body
