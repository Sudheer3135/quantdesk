"""Strategy v2's API: read the paper account, close by hand behind the key."""
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import get_db
from app.strategy_v2 import paper
from test_v2_paper import (  # noqa: F401
    Clock,
    Desk,
    buy_signal,
    desk,  # noqa: F401  (fixture)
)


@pytest.fixture
def api(desk, db, monkeypatch):  # noqa: F811
    from app.api import strategy_v2
    trader = desk.trader()
    monkeypatch.setattr(paper, "TRADER", trader)
    app = FastAPI()
    app.include_router(strategy_v2.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), trader


def test_status_reports_a_paper_account(api):
    client, _ = api
    body = client.get("/v2/status").json()
    assert body["mode"] == "paper"
    assert body["account"]["equity"] == 350_000
    assert body["position"] is None
    assert body["config"]["premium_stop_pct"] == 30.0


def test_positions_and_decisions_are_listed_after_a_trade(api, desk):  # noqa: F811
    client, trader = api
    desk.signal = buy_signal()
    trader.step()
    trader.close_now()

    listed = client.get("/v2/positions").json()
    assert listed["summary"]["closed"] == 1
    assert listed["positions"][0]["exit_reason"] == "manual"

    today = client.get("/v2/decisions", params={"day": date(2026, 9, 17).isoformat()}).json()
    assert [d["code"] for d in today["decisions"]] == ["entered"]


def test_closing_needs_the_key_when_one_is_set(api, desk, monkeypatch):  # noqa: F811
    from app.config import get_settings
    client, trader = api
    desk.signal = buy_signal()
    trader.step()

    monkeypatch.setenv("API_KEY", "secret")
    get_settings.cache_clear()
    assert client.post("/v2/close").status_code == 401
    assert client.post("/v2/close", headers={"X-API-Key": "secret"}).status_code == 200
    assert client.post("/v2/close", headers={"X-API-Key": "secret"}).status_code == 404
    get_settings.cache_clear()


def test_a_bad_status_filter_is_refused(api):
    client, _ = api
    assert client.get("/v2/positions", params={"status": "pending"}).status_code == 422
