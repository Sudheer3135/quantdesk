"""Tests for the database-first API surface.

The behaviour under test is a refusal. When the archive cannot cover the
requested window, `/backtest/run` must fail loudly rather than quietly
backtest a shorter one — because a six-week result and a two-year result
look identical in the response, and you would act on either.
"""
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import backtest as backtest_api
from app.api import data as data_api
from app.data.importer import import_index_candles
from app.db import get_db
from test_importer import session_bars

# Weekdays in June 2026, avoiding weekends.
TRADING_DAYS = [16, 17, 18, 19, 22, 23, 24, 25, 26]


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.include_router(data_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def seed(db, days=TRADING_DAYS, bars=75):
    for day in days:
        import_index_candles(db, session_bars(date(2026, 6, day), count=bars),
                             "NIFTY", "5m", "test")


# ---- the refusal ------------------------------------------------------

def test_an_empty_archive_returns_409_not_an_empty_backtest(client):
    response = client.post("/backtest/run", json={"symbol": "NIFTY", "timeframe": "5m"})
    assert response.status_code == 409

    detail = response.json()["detail"]
    assert detail["error"] == "insufficient coverage"
    assert "no candles stored" in detail["reason"]
    assert "/data/import/index" in detail["fix"]


def test_asking_for_more_history_than_exists_returns_409(client, db):
    """The failure this whole phase exists to remove."""
    seed(db)
    response = client.post("/backtest/run", json={
        "symbol": "NIFTY", "timeframe": "5m",
        "start": "2020-01-01", "end": "2026-06-26",
    })
    assert response.status_code == 409

    detail = response.json()["detail"]
    assert "history begins at" in detail["reason"]
    assert detail["available"]["sessions"] == len(TRADING_DAYS)


def test_too_few_sessions_returns_409_even_when_the_range_fits(client, db):
    """Three sessions inside the window is technically coverage and is not
    enough for any statistic to mean anything."""
    seed(db, days=[16, 17, 18])
    response = client.post("/backtest/run", json={"min_sessions": 5})
    assert response.status_code == 409
    assert "session" in response.json()["detail"]["reason"]


def test_the_409_names_the_command_that_fixes_it(client):
    """An error that does not say what to do next gets worked around rather
    than fixed, usually by whatever reintroduces the original problem."""
    detail = client.post("/backtest/run", json={}).json()["detail"]
    assert detail["fix"].startswith("POST /data/import/index")


# ---- the happy path ---------------------------------------------------

def test_a_covered_window_runs_and_names_its_data(client, db):
    seed(db)
    response = client.post("/backtest/run", json={
        "symbol": "NIFTY", "timeframe": "5m", "starting_capital": 200_000,
    })
    assert response.status_code == 200

    body = response.json()
    assert body["dataset"]["mode"] == "db"
    assert body["dataset"]["row_count"] == 75 * len(TRADING_DAYS)
    assert body["dataset"]["session_count"] == len(TRADING_DAYS)
    assert len(body["dataset"]["hash"]) == 64
    assert body["assumptions"]["costs"]["kind"] == "flat"


def test_the_same_window_twice_yields_the_same_hash(client, db):
    """Reproducibility, end to end. Without this a result cannot be compared
    with any other result."""
    seed(db)
    payload = {"symbol": "NIFTY", "timeframe": "5m"}
    first = client.post("/backtest/run", json=payload).json()
    second = client.post("/backtest/run", json=payload).json()

    assert first["dataset"]["hash"] == second["dataset"]["hash"]
    assert first["stats"] == second["stats"]


def test_growing_the_archive_changes_the_hash(client, db):
    """The other half: a hash that does not move when the data moves would
    certify something false."""
    seed(db)
    before = client.post("/backtest/run", json={}).json()["dataset"]["hash"]

    import_index_candles(db, session_bars(date(2026, 6, 29), count=75),
                         "NIFTY", "5m", "test")
    after = client.post("/backtest/run", json={}).json()["dataset"]["hash"]

    assert before != after


def test_a_date_range_narrows_the_dataset(client, db):
    seed(db)
    body = client.post("/backtest/run", json={
        "start": "2026-06-17", "end": "2026-06-23", "min_sessions": 3,
    }).json()
    # Wed 17, Thu 18, Fri 19, Mon 22, Tue 23 — the weekend is not a session.
    assert body["dataset"]["session_count"] == 5
    assert body["dataset"]["row_count"] == 75 * 5


def test_every_run_is_registered_so_it_can_be_looked_up_later(client, db):
    seed(db)
    client.post("/backtest/run", json={})
    listed = client.get("/data/datasets").json()["datasets"]
    assert len(listed) == 1
    assert listed[0]["rows"] == 75 * len(TRADING_DAYS)


def test_mock_data_carries_a_caveat_into_the_result(client, db):
    """A backtest over a random walk describes noise, and the result has to
    say so where somebody reading the statistics will see it."""
    for day in TRADING_DAYS:
        import_index_candles(db, session_bars(date(2026, 6, day), count=75),
                             "NIFTY", "5m", "mock")

    body = client.post("/backtest/run", json={}).json()
    assert any("random walk" in c for c in body["dataset"]["caveats"])


# ---- the /data endpoints ----------------------------------------------

def test_coverage_reports_what_is_held(client, db):
    seed(db)
    body = client.get("/data/coverage").json()
    assert body["rows"] == 75 * len(TRADING_DAYS)
    assert body["sessions"] == len(TRADING_DAYS)
    assert body["sources"] == {"test": 75 * len(TRADING_DAYS)}


def test_coverage_on_an_empty_archive_says_what_to_run(client):
    body = client.get("/data/coverage").json()
    assert body["rows"] == 0
    assert "/data/import/index" in body["note"]


def test_quality_reports_a_verdict(client, db):
    seed(db)
    body = client.get("/data/quality").json()
    assert body["verdict"] in {"clean", "usable with caveats", "unusable"}
    assert isinstance(body["findings"], list)


def test_an_option_import_without_an_expiry_is_refused(client):
    """The chain endpoint silently defaults to the nearest expiry. Filing a
    snapshot under a guess merges two contracts into one premium series and
    the result looks entirely plausible."""
    response = client.post("/data/import/options", params={"symbol": "NIFTY"})
    assert response.status_code == 400
    assert "expiry is required" in response.json()["detail"]
