"""`/market/vix` — the number the dashboard tile shows.

v2's own risk gate has read the Angel-streamed VIX from day one, sub-second
fresh. This endpoint did not: it always called `broker.india_vix()`, which
polls NSE directly, on every request, with no cache — measured elsewhere in
this codebase at refreshing roughly once a minute. These tests pin the fix:
the stream wins whenever it has a reading fresh enough to trust, and NSE is
never even called in that case.
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import market as market_api
from app.workers import vix_live


class CountingBroker:
    """Counts how many times the poll path is actually reached."""

    def __init__(self, value=11.5):
        self.value = value
        self.calls = 0

    def india_vix(self):
        self.calls += 1
        return self.value


@pytest.fixture
def broker():
    return CountingBroker()


@pytest.fixture
def client(broker, monkeypatch):
    monkeypatch.setattr(market_api, "get_broker", lambda: broker)
    app = FastAPI()
    app.include_router(market_api.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_vix_store():
    """`vix_live.VIX` is a process-wide singleton; leave it as found."""
    saved = vix_live.VIX._latest          # noqa: SLF001 — test isolation only
    yield
    vix_live.VIX._latest = saved          # noqa: SLF001


def test_the_stream_wins_when_it_is_fresh(client, broker, monkeypatch):
    monkeypatch.setattr(vix_live.VIX, "current", lambda **_: 13.37)

    body = client.get("/market/vix").json()

    assert body == {"india_vix": 13.37, "source": "angel", "transport": "stream"}
    assert broker.calls == 0, "NSE must not be polled when the stream is fresh"


def test_the_poll_is_the_fallback_when_the_stream_has_nothing_fresh(
        client, broker, monkeypatch):
    monkeypatch.setattr(vix_live.VIX, "current", lambda **_: None)

    body = client.get("/market/vix").json()

    assert body["india_vix"] == broker.value
    assert body["transport"] == "poll"
    assert broker.calls == 1


def test_a_stale_stream_reading_is_not_served_as_current(client, monkeypatch):
    """`VixStore.current` is what decides freshness; this pins that a
    reading older than its own ceiling is treated as absent, not stale-but-
    good-enough — the same standard the price feed holds itself to."""
    from datetime import UTC, datetime, timedelta

    from app.workers.vix_live import VixReading

    now = datetime(2026, 9, 15, 8, 0, 0, tzinfo=UTC)
    old = now - timedelta(seconds=vix_live.MAX_AGE_SECONDS + 1)
    vix_live.VIX._latest = VixReading(value=99.0, source_time=old, received_at=old)  # noqa: SLF001
    monkeypatch.setattr(vix_live.VIX, "_now", lambda: now)

    body = client.get("/market/vix").json()

    assert body["transport"] == "poll", "a stale reading must fall back, not serve 99.0"
