"""Keeping the regime table current, and getting it onto the screen.

Two ends of the same requirement. A regime table that only advances when
someone runs a backfill by hand is a research artefact, so the agent
classifies each bar as it archives it; and the dashboard has to be able to
show the result over the socket it already holds open.

The classification is deliberately guarded separately from the candle
import. Candles on a free source cannot be re-fetched later, so a classifier
failure must cost the desk its labels and never its history —
`test_a_classifier_failure_does_not_cost_the_candles` is that guarantee.
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

from app.api import stream
from app.data import regime_store
from app.market_hours import IST
from app.models import CandleRecord, MarketRegime
from app.workers import agent
from sqlalchemy import select

BARS_PER_SESSION = 75


class SessionFactory:
    """Hands the worker the test's own session without closing it."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def candles(n=200, seed=4):
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
    return pd.DataFrame({
        "timestamp": stamps, "open": close - steps,
        "high": np.maximum(close, close - steps) + 8,
        "low": np.minimum(close, close - steps) - 8,
        "close": close, "volume": [1000.0] * n})


class StubBroker:
    def __init__(self, frame):
        self.frame = frame

    def candles(self, *a, **k):
        return self.frame

    def option_chain(self, *a, **k):
        return None

    def india_vix(self):
        return None


@pytest.fixture
def ticking(db, monkeypatch):
    """An agent tick wired to this test's session and a canned candle feed."""
    frame = candles()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "get_broker", lambda: StubBroker(frame))
    # Pinned closed. Every test below either passes `force=True` to bypass
    # the gate deliberately or asserts the gate holds, so nothing here should
    # depend on what time CI happens to run.
    monkeypatch.setattr(agent, "market_is_open", lambda: False)
    monkeypatch.setattr(agent, "publish", lambda *a, **k: None)
    # The analysis half of the tick is not what is under test here, and
    # letting it run would drag the whole engine and its option chain into a
    # test about bookkeeping. It raises rather than returning a stub so that
    # nothing downstream of it can quietly become part of what these tests
    # cover.
    monkeypatch.setattr(agent, "build_analysis",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("skip")))
    return frame


# ---- the agent keeps the table current ---------------------------------

def test_a_tick_classifies_the_bars_it_just_archived(db, ticking):
    agent.tick(force=True)

    stored = db.scalars(select(MarketRegime)).all()
    assert stored
    assert all(r.day_regime for r in stored)
    assert all(r.engine_version for r in stored)


def test_the_tick_agrees_with_a_full_backfill(db, ticking):
    """The incremental path and the research path must not drift apart."""
    agent.tick(force=True)
    incremental = {r.timestamp: (r.day_regime, r.day_confidence)
                   for r in db.scalars(select(MarketRegime)).all()}

    regime_store.clear(db, "NIFTY", "5m")
    regime_store.backfill(db, "NIFTY", "5m")
    full = {r.timestamp: (r.day_regime, r.day_confidence)
            for r in db.scalars(select(MarketRegime)).all()}

    for stamp, verdict in incremental.items():
        assert full[stamp] == verdict


def test_repeated_ticks_do_not_duplicate_rows(db, ticking):
    agent.tick(force=True)
    first = len(db.scalars(select(MarketRegime)).all())
    agent.tick(force=True)

    assert len(db.scalars(select(MarketRegime)).all()) == first


def test_a_classifier_failure_does_not_cost_the_candles(db, ticking, monkeypatch):
    """History on a free source cannot be re-fetched. A label can always be
    recomputed, so the two failures must not share a guard."""
    monkeypatch.setattr(
        regime_store, "refresh_recent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("classifier broke")))

    agent.tick(force=True)

    assert db.scalars(select(CandleRecord)).all()
    assert db.scalars(select(MarketRegime)).all() == []


def test_a_shut_market_classifies_nothing(db, ticking, monkeypatch):
    """The tick is session-gated, and classification sits inside that gate.

    The session is stubbed rather than left to the wall clock: this suite
    runs in CI at whatever hour the push happens, and a test whose result
    depends on that is a test that fails once a day for reasons unrelated to
    the code. The same lesson the consecutive-loss test learned at 00:49 IST.
    """
    monkeypatch.setattr(agent, "market_is_open", lambda: False)

    agent.tick()

    assert db.scalars(select(MarketRegime)).all() == []
    assert db.scalars(select(CandleRecord)).all() == []


# ---- and it reaches the dashboard --------------------------------------

@pytest.fixture
def socket(db, monkeypatch):
    app = FastAPI()
    app.include_router(stream.router)
    monkeypatch.setattr(stream, "get_json", lambda key: None)
    monkeypatch.setattr(stream, "SessionLocal", SessionFactory(db))
    return TestClient(app)


def test_the_snapshot_carries_the_current_regime(db, socket):
    from app.data.importer import import_index_candles
    import_index_candles(db, candles(), "NIFTY", "5m", source="test")
    regime_store.backfill(db, "NIFTY", "5m")

    with socket.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["type"] == "snapshot"
    assert msg["regime"]["day"]["label"]
    assert msg["regime"]["day"]["reasons"]


def test_the_snapshot_reports_no_regime_rather_than_inventing_one(db, socket):
    with socket.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["regime"] is None


def test_a_broken_regime_read_does_not_break_the_socket(db, socket, monkeypatch):
    """A regime is a caption. Losing it must not cost the desk its feed."""
    monkeypatch.setattr(
        regime_store, "latest",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))

    with socket.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["type"] == "snapshot"
    assert msg["regime"] is None
    assert "market" in msg
