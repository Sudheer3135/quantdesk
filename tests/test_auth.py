"""API-key access control.

Audit finding H-2: every endpoint was open, including the nine that write to
Postgres. These tests pin the boundary that fixed it — reads open, writes
keyed — and the two cases that are easy to get wrong: a GET that writes
because of a query parameter, and a websocket that cannot carry a header.
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import security
from app.api import stream
from app.config import get_settings
from app.security import HEADER, AuthNotConfigured, key_is_valid, verify_startup

KEY = "test-key-do-not-reuse"


@pytest.fixture
def keyed(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("API_KEY", KEY)
    yield KEY
    get_settings.cache_clear()


@pytest.fixture
def open_desk(monkeypatch):
    """A desk with no key configured.

    Set to empty rather than deleted: `Settings` falls back to the .env file
    when the variable is absent, so deleting it hands control back to
    whatever the developer happens to have on disk. Empty is unambiguous and
    is treated as unset by `configured_key`.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("API_KEY", "")
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# the key itself
# ---------------------------------------------------------------------------

def test_no_key_configured_means_the_layer_stands_aside(open_desk):
    """A personal desk on localhost should not need a key to run at all."""
    assert security.auth_enabled() is False
    assert key_is_valid(None) is True
    assert key_is_valid("anything") is True


def test_a_configured_key_is_required_and_compared_exactly(keyed):
    assert security.auth_enabled() is True
    assert key_is_valid(KEY) is True
    assert key_is_valid(None) is False
    assert key_is_valid("") is False
    assert key_is_valid(KEY + "x") is False
    assert key_is_valid(KEY.upper()) is False


def test_empty_string_is_treated_as_unset(monkeypatch):
    """A blank environment variable is a missing key, not a key of ''."""
    get_settings.cache_clear()
    monkeypatch.setenv("API_KEY", "")
    try:
        assert security.configured_key() is None
        assert security.auth_enabled() is False
    finally:
        get_settings.cache_clear()


def test_production_refuses_to_boot_without_a_key(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("API_KEY", "")
    try:
        with pytest.raises(AuthNotConfigured, match="requires API_KEY"):
            verify_startup()
    finally:
        get_settings.cache_clear()


def test_production_boots_with_a_key(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("API_KEY", KEY)
    try:
        verify_startup()
    finally:
        get_settings.cache_clear()


def test_development_boots_open(open_desk):
    verify_startup()


# ---------------------------------------------------------------------------
# the websocket
# ---------------------------------------------------------------------------

def ws_app():
    app = FastAPI()
    app.include_router(stream.router)
    return TestClient(app)


def test_socket_is_open_when_no_key_is_configured(open_desk, monkeypatch):
    monkeypatch.setattr(stream, "get_json", lambda key: None)
    with ws_app().websocket_connect("/ws/signals") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_socket_accepts_the_key_as_a_query_parameter(keyed, monkeypatch):
    """Browsers cannot set headers on a websocket handshake."""
    monkeypatch.setattr(stream, "get_json", lambda key: None)
    with ws_app().websocket_connect(f"/ws/signals?key={KEY}") as ws:
        assert ws.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize("suffix", ["", "?key=", "?key=wrong"])
def test_socket_refuses_a_missing_or_wrong_key(keyed, suffix):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with ws_app().websocket_connect(f"/ws/signals{suffix}") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


# ---------------------------------------------------------------------------
# the write endpoints
# ---------------------------------------------------------------------------

def journal_app(db):
    """The journal router wired to the test database."""
    from app.api import journal
    from app.db import get_db

    app = FastAPI()
    app.include_router(journal.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


TRADE = {"symbol": "NIFTY", "side": "BUY", "quantity": 75,
         "entry": 24200.0, "stop_loss": 24190.0}


def test_write_is_refused_without_a_key(keyed, db):
    r = journal_app(db).post("/journal", json=TRADE)
    assert r.status_code == 401
    assert HEADER in r.json()["detail"]


def test_write_is_refused_with_a_wrong_key(keyed, db):
    r = journal_app(db).post("/journal", json=TRADE, headers={HEADER: "wrong"})
    assert r.status_code == 401


def test_write_succeeds_with_the_key(keyed, db):
    r = journal_app(db).post("/journal", json=TRADE, headers={HEADER: KEY})
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def test_reads_stay_open_even_when_a_key_is_configured(keyed, db):
    """The dashboard polls these; keying them would push the secret into the
    browser for no gain, since they expose nothing not already on screen."""
    r = journal_app(db).get("/journal")
    assert r.status_code == 200


def test_writes_are_open_when_no_key_is_configured(open_desk, db):
    r = journal_app(db).post("/journal", json=TRADE)
    assert r.status_code == 200


def walk_routes(routes):
    """Every route, flattened.

    FastAPI changed this shape: up to 0.115 `app.routes` was already flat,
    while newer versions wrap each `include_router` call in an
    `_IncludedRouter` whose children hang off `original_router`. Walking
    both means this test cannot silently pass by finding nothing — which is
    exactly what it did on the newer stack before this was fixed.
    """
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from walk_routes(nested.routes)
        else:
            yield route


def test_the_route_walk_actually_finds_routes():
    """Guards the guard. If this returns nothing, the test below is vacuous."""
    from app.main import app as real_app

    paths = {getattr(r, "path", None) for r in walk_routes(real_app.routes)}
    assert "/journal" in paths
    assert "/data/import/index" in paths


def test_every_mutating_route_carries_the_guard():
    """Structural: a new POST added without the dependency should fail here
    rather than quietly ship an unprotected write."""
    from app.main import app as real_app
    from app.security import require_api_key

    unguarded = []
    for route in walk_routes(real_app.routes):
        methods = getattr(route, "methods", set()) or set()
        if not ({"POST", "PUT", "PATCH", "DELETE"} & methods):
            continue
        deps = getattr(route, "dependencies", [])
        if not any(d.dependency is require_api_key for d in deps):
            unguarded.append(f"{sorted(methods)} {route.path}")
    assert unguarded == [], f"unprotected mutating routes: {unguarded}"
