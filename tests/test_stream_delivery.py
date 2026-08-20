"""The delivery path: Redis to browser.

Measurement showed this stage is not where the latency was — median 8ms
from publish to receive. These tests are here to keep it that way, and to
pin down two things the measurement could not:

  - a browser that connects between ticks is handed the current price with
    its age recalculated as of *now*, not the age it had when published;
  - a price frame reaches the socket without the client asking for it, so
    nothing on the dashboard depends on a refresh button.

The websocket is driven through FastAPI's TestClient against the real
router, so the framing and message shapes under test are the ones a browser
actually receives.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import stream


@pytest.fixture
def client(monkeypatch):
    """The stream router alone, with Redis reads stubbed.

    The relay task needs a real Redis to subscribe to, so these tests cover
    the snapshot-on-connect path and the shapes; the fan-out itself is
    exercised directly against `hub.broadcast` below.
    """
    app = FastAPI()
    app.include_router(stream.router)
    return TestClient(app)


def stub_cache(monkeypatch, price=None, signal=None):
    def get_json(key):
        return {"price:latest": price, "signal:latest": signal}.get(key)
    monkeypatch.setattr(stream, "get_json", get_json)


def make_price(age_seconds, value=24_223.35):
    printed = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {
        "symbol": "NIFTY", "price": value, "previous": value - 1.5,
        "change": 1.5, "direction": "up", "source": "yahoo",
        "source_time": printed.isoformat(),
        # Deliberately wrong: the age as it was at publish time. A client
        # that trusted this field would under-report staleness forever.
        "age_seconds": 0.1, "freshness": "live",
        "at": printed.isoformat(), "market_open": True,
    }


# ---------------------------------------------------------------------------
# snapshot on connect
# ---------------------------------------------------------------------------

def test_connecting_browser_immediately_receives_the_current_price(monkeypatch, client):
    """No waiting for the next tick, and no refresh button."""
    stub_cache(monkeypatch, price=make_price(age_seconds=3))

    with client.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["type"] == "snapshot"
    assert msg["price"]["price"] == 24_223.35
    assert msg["price"]["freshness"] == "live"


def test_snapshot_ages_the_cached_price_as_of_now(monkeypatch, client):
    """A price published two minutes ago is two minutes old, whatever the
    cached blob claims. This is the bug that made a dead feed look alive."""
    stale = make_price(age_seconds=134)
    assert stale["freshness"] == "live"          # what the cache still says

    stub_cache(monkeypatch, price=stale)

    with client.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["price"]["freshness"] == "stale"
    assert msg["price"]["age_seconds"] == pytest.approx(134, abs=2)


def test_snapshot_survives_an_empty_cache(monkeypatch, client):
    """A cold start must not break the socket."""
    stub_cache(monkeypatch, price=None, signal=None)

    with client.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["type"] == "snapshot"
    assert msg["price"] is None


def test_undated_cached_price_is_reported_unknown(monkeypatch, client):
    price = make_price(age_seconds=5)
    price["source_time"] = None
    stub_cache(monkeypatch, price=price)

    with client.websocket_connect("/ws/signals") as ws:
        msg = ws.receive_json()

    assert msg["price"]["freshness"] == "unknown"
    assert msg["price"]["age_seconds"] is None


# ---------------------------------------------------------------------------
# push delivery
# ---------------------------------------------------------------------------

def test_a_published_price_is_pushed_to_every_client_unprompted(monkeypatch):
    """The delivery guarantee behind "no manual refresh".

    Two connected sockets, one broadcast, both receive it — with no request
    from either. `hub.broadcast` is the exact call the Redis relay makes on
    each price message.
    """
    import asyncio

    sent = {"a": [], "b": []}

    class FakeSocket:
        def __init__(self, name):
            self.name = name

        async def send_json(self, message):
            sent[self.name].append(message)

    hub = stream.Hub()
    hub.clients = {FakeSocket("a"), FakeSocket("b")}

    frame = {"type": "price", "price": make_price(age_seconds=1)}
    asyncio.run(hub.broadcast(frame))

    assert sent["a"] == [frame]
    assert sent["b"] == [frame]


def test_broadcast_drops_a_dead_socket_without_starving_the_others(monkeypatch):
    """One browser closing its tab must not stop the tape for everyone else."""
    import asyncio

    delivered = []

    class Healthy:
        async def send_json(self, message):
            delivered.append(message)

    class Broken:
        async def send_json(self, message):
            raise RuntimeError("client gone")

    hub = stream.Hub()
    healthy, broken = Healthy(), Broken()
    hub.clients = {healthy, broken}

    asyncio.run(hub.broadcast({"type": "price", "price": make_price(1)}))

    assert len(delivered) == 1
    assert broken not in hub.clients
    assert healthy in hub.clients


# ---------------------------------------------------------------------------
# the HTTP fallback tells the same story
# ---------------------------------------------------------------------------

def test_http_endpoint_ages_the_price_at_read_time(monkeypatch, client):
    stub_cache(monkeypatch, price=make_price(age_seconds=40))

    body = client.get("/market/price").json()

    assert body["freshness"] == "delayed"
    assert body["age_seconds"] == pytest.approx(40, abs=2)
    assert body["price"] == 24_223.35


def test_http_and_socket_agree_about_freshness(monkeypatch, client):
    """Two views of one desk must not disagree about whether the data is
    trustworthy."""
    stub_cache(monkeypatch, price=make_price(age_seconds=200))

    over_http = client.get("/market/price").json()
    with client.websocket_connect("/ws/signals") as ws:
        over_socket = ws.receive_json()["price"]

    assert over_http["freshness"] == over_socket["freshness"] == "stale"


def test_empty_cache_reports_unknown_rather_than_a_price_of_none(monkeypatch, client):
    stub_cache(monkeypatch, price=None)

    body = client.get("/market/price").json()

    assert body["price"] is None
    assert body["freshness"] == "unknown"
    assert body["age_seconds"] is None
