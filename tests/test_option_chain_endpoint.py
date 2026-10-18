"""The option-chain endpoint's upstream budget.

The collector and this endpoint share one throttled NSE session, and they
are not equally important. A dashboard request can be answered from cache;
a snapshot the collector misses is gone for good, because NSE publishes no
option history to backfill from. So the rule under test is that a browser
can never spend the collector's budget.

Before the gate these tests cover, a dashboard left open overnight issued
one live NSE call every sixty seconds until somebody closed the tab: the
cache TTL was forty-five seconds against a sixty-second refresh, so it
expired before every single poll and served none of them. Across one
retained log, 662 of 1,272 upstream calls were made after the close.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import market as market_api

# One row per strike carrying both sides, which is the shape `summarise`
# validates against — not one row per contract.
CHAIN = pd.DataFrame([
    {"strike": 24100, "call_oi": 800, "put_oi": 1500, "call_ltp": 180.0,
     "put_ltp": 70.0, "call_iv": 12.1, "put_iv": 13.0},
    {"strike": 24200, "call_oi": 1000, "put_oi": 900, "call_ltp": 120.0,
     "put_ltp": 110.0, "call_iv": 12.5, "put_iv": 12.9},
    {"strike": 24300, "call_oi": 1700, "put_oi": 600, "call_ltp": 75.0,
     "put_ltp": 165.0, "call_iv": 12.8, "put_iv": 12.4},
])


class CountingBroker:
    """Counts how many times the endpoint actually reaches upstream."""

    def __init__(self):
        self.chain_calls = 0

    def option_chain(self, symbol, expiry=None):
        self.chain_calls += 1
        frame = CHAIN.copy()
        frame.attrs["expiry"] = "01-Sep-2026"
        return frame

    def quote(self, symbol):
        return {"last_price": 24210.0}


@pytest.fixture
def broker():
    return CountingBroker()


@pytest.fixture
def client(broker, monkeypatch):
    # The endpoint calls `get_broker()` directly rather than through
    # `Depends`, so `dependency_overrides` would not reach it.
    monkeypatch.setattr(market_api, "get_broker", lambda: broker)
    app = FastAPI()
    app.include_router(market_api.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def memory_cache(monkeypatch):
    """An in-process stand-in for Redis, so TTLs are exercised not mocked."""
    store: dict[str, object] = {}

    def get_json(key):
        return store.get(key)

    def set_json(key, value, ttl=60):
        store[key] = value

    monkeypatch.setattr(market_api, "get_json", get_json)
    monkeypatch.setattr(market_api, "set_json", set_json)
    return store


def open_market(monkeypatch, is_open=True):
    monkeypatch.setattr(market_api, "market_is_open", lambda: is_open)


def test_an_open_market_fetches_a_live_chain(client, broker, monkeypatch):
    open_market(monkeypatch)
    body = client.get("/market/option-chain?symbol=NIFTY").json()

    assert broker.chain_calls == 1
    assert body["live"] is True
    assert body["fetched_at"]
    assert body["summary"]


def test_a_closed_market_never_reaches_upstream(client, broker, monkeypatch):
    """The whole point. An overnight dashboard must cost nothing."""
    open_market(monkeypatch)
    client.get("/market/option-chain?symbol=NIFTY")          # one live fetch
    assert broker.chain_calls == 1

    open_market(monkeypatch, is_open=False)
    for _ in range(20):                                       # a night of polling
        response = client.get("/market/option-chain?symbol=NIFTY")
        assert response.status_code == 200

    assert broker.chain_calls == 1, "a closed market must not call NSE"


def test_the_closed_market_answer_says_it_is_not_live(client, monkeypatch,
                                                      memory_cache):
    open_market(monkeypatch)
    client.get("/market/option-chain?symbol=NIFTY")

    # Expire the short-lived cache, leaving only the long-lived last-good.
    memory_cache.pop("chain:NIFTY:None")

    open_market(monkeypatch, is_open=False)
    body = client.get("/market/option-chain?symbol=NIFTY").json()

    assert body["live"] is False, "stale data must never be labelled current"
    assert body["summary"], "the last good chain is still served"


def test_a_cold_cache_out_of_hours_fetches_once_and_only_once(
        client, broker, monkeypatch):
    """A dashboard opened out of hours should not be blank.

    With nothing cached there is nothing to serve, so one call goes out —
    and is then cached for a day, so a tab left open overnight cannot turn
    that single bootstrap into a poll.
    """
    open_market(monkeypatch, is_open=False)

    bodies = [client.get("/market/option-chain?symbol=NIFTY").json()
              for _ in range(10)]

    assert broker.chain_calls == 1, "only the first request may reach upstream"
    assert all(b["live"] is False for b in bodies), (
        "a chain fetched while the market is shut is last session's, "
        "and must not be labelled live")


def test_the_cache_outlives_the_dashboard_refresh(client, broker, monkeypatch):
    """A sixty-second refresh must hit a cache, not the exchange.

    This is the regression that mattered: at ttl=45 against a 60s poll the
    cache expired before every request and served none of them.
    """
    assert market_api.CHAIN_TTL_SECONDS > 60

    open_market(monkeypatch)
    for _ in range(5):
        client.get("/market/option-chain?symbol=NIFTY")

    assert broker.chain_calls == 1, "repeat polls must be served from cache"


def test_the_last_good_chain_outlives_a_weekend(client, monkeypatch):
    """Friday's close has to answer Monday's pre-open dashboard."""
    assert market_api.CHAIN_LAST_TTL_SECONDS >= 24 * 3600
