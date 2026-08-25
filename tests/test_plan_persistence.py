"""The two-layer read reaching the database and the dashboard.

Both routes that persist a signal have to store the same three columns. The
last time a field was assembled separately in these two places, the agent's
route — the one the dashboard actually reads — shipped without it for weeks
and nothing reported the gap. That was audit finding H-4, and
`test_both_writers_store_the_plan_the_same_way` is the guard against a
repeat.

The other claim under test is that the plan is additive. A signal is the
product; the plan is a caption on it. If building the caption fails, the
signal still has to ship.
"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import plan as plan_builder
from app.analytics import signal_engine
from app.api import signals as signals_api
from app.api.signals import Analysis
from app.config import get_settings
from app.db import get_db
from app.market_hours import IST
from app.models import SignalRecord
from app.workers import agent

BARS_PER_SESSION = 75


class SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def candles(n=400, seed=6):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 6.0, n)
    close = 24_000 + np.cumsum(steps)
    stamps, day = [], datetime(2026, 6, 1, 9, 15, tzinfo=IST)
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


def a_signal(action="BUY"):
    return signal_engine.Signal(
        symbol="NIFTY", timeframe="5m",
        timestamp=datetime.now(UTC).isoformat(), action=action,
        confidence=0.62, price=24_200.0, entry=24_200.0,
        stop_loss=24_190.0, target=24_225.0, risk_reward=2.5,
        checks=[], context={})


def a_plan():
    return plan_builder.build(candles())


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# ---- the columns -------------------------------------------------------

def test_the_endpoint_stores_bias_and_entry_state(client, db, monkeypatch):
    built = a_plan()
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_signal(), plan=built))

    body = client.get("/signals/live?persist=true").json()
    record = db.scalars(select(SignalRecord)).one()

    assert record.bias == built.bias["label"]
    assert record.entry_state == built.entry["state"]
    assert record.plan["bias"]["reasons"]
    assert body["plan"]["entry"]["state"] == built.entry["state"]


def test_the_agent_stores_bias_and_entry_state(db, monkeypatch):
    built = a_plan()
    sent = {}
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish",
                        lambda channel, blob, **kw: sent.update(
                            payload=json.loads(blob)))
    monkeypatch.setattr(agent, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_signal(), plan=built))

    agent.tick(force=True)
    record = db.scalars(select(SignalRecord)).one()

    assert record.bias == built.bias["label"]
    assert record.entry_state == built.entry["state"]
    assert sent["payload"]["plan"]["bias"]["label"] == built.bias["label"]


def test_both_writers_store_the_plan_the_same_way(db, monkeypatch):
    """One definition of the plan columns, used by both routes.

    Audit finding H-4 was exactly this shape: a field assembled separately in
    the endpoint and the agent, present in one and silently missing from the
    other for weeks.
    """
    built = a_plan()
    columns = signals_api.plan_columns(built)

    assert set(columns) == {"bias", "entry_state", "plan"}
    assert columns["bias"] in plan_builder.BIAS_LABELS
    assert columns["entry_state"] in plan_builder.ENTRY_STATES


def test_a_missing_plan_stores_nulls_rather_than_a_guess(db):
    columns = signals_api.plan_columns(None)

    assert columns == {"bias": None, "entry_state": None, "plan": None}


# ---- additive, never load-bearing --------------------------------------

def test_a_failing_plan_does_not_cost_the_signal(monkeypatch, db):
    """The signal is the product; the plan is a caption on it."""
    monkeypatch.setattr(
        signals_api.plan_builder, "build",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plan broke")))
    monkeypatch.setattr(signals_api.signal_engine, "generate",
                        lambda *a, **k: a_signal())

    analysis = signals_api.build_analysis("NIFTY", "5m")

    assert analysis.signal.action == "BUY"
    assert analysis.plan is None


def test_a_signal_without_a_plan_still_persists(client, db, monkeypatch):
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_signal(), plan=None))

    body = client.get("/signals/live?persist=true").json()
    record = db.scalars(select(SignalRecord)).one()

    assert body["plan"] is None
    assert record.bias is None
    assert record.action == "BUY"


def test_the_signal_and_the_plan_describe_the_same_bar(monkeypatch):
    """Built from one fetch. Two broker calls could straddle a five-minute
    boundary and put a BUY next to a plan formed on a different price."""
    frame = candles()
    seen = []

    class Broker:
        def candles(self, *a, **k):
            seen.append(1)
            return frame

        def option_chain(self, *a, **k):
            return None

        def india_vix(self):
            return None

    monkeypatch.setattr(signals_api, "get_broker", lambda: Broker())
    analysis = signals_api.build_analysis("NIFTY", "5m")

    assert len(seen) == 1
    assert analysis.plan.timestamp == analysis.signal.timestamp


def test_build_signal_still_returns_just_the_signal(monkeypatch):
    """Plenty of callers only want the verdict."""
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_signal(), plan=a_plan()))

    assert signals_api.build_signal("NIFTY", "5m").action == "BUY"


# ---- and onto the wire -------------------------------------------------

def test_the_published_payload_carries_both_layers(db, monkeypatch):
    built = a_plan()
    sent = {}
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish",
                        lambda channel, blob, **kw: sent.update(
                            payload=json.loads(blob)))
    monkeypatch.setattr(agent, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_signal(), plan=built))

    agent.tick(force=True)
    payload = sent["payload"]

    assert payload["plan"]["bias"]["label"] in plan_builder.BIAS_LABELS
    assert payload["plan"]["entry"]["state"] in plan_builder.ENTRY_STATES
    assert payload["plan"]["entry"]["reasons"]
    # The risk decision still rides alongside, unchanged.
    assert payload["risk"]["state"] in ("approved", "blocked")
