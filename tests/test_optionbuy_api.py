"""The option-buying endpoints, including the refusals.

`/backtest/run` already answers 409 rather than quietly running a shorter
window than you asked for. This is that rule for option history, which needs
it more: index candles can be re-fetched, and a session the option collector
missed is gone permanently.

Two refusals are tested, and they are different questions. The coverage gate
is about *sessions* and runs before the walk. The evidence gate is about the
*trades actually taken* and runs after it — a window can clear the first and
fail the second when every trade lands in the uncovered part of it.
"""
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import backtest as backtest_api
from app.db import get_db
from app.optionbuy import runner
from app.optionbuy.contracts import SelectionConfig
from app.optionbuy.pricing import MODELLED, MODELLED_ONLY, OBSERVED_ONLY
from app.optionbuy.strategy import OptionBuyConfig
from optionbuy_fixtures import (
    candles,
    seed_index_rows,
    seed_option_rows,
    sessions,
)

DAYS = sessions(date(2025, 6, 2), 6)
EXPIRY = date(2025, 6, 17)
STRIKES = [23_900.0, 23_950.0, 24_000.0, 24_050.0, 24_100.0]


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# Short index sessions keep the walk quick. The option archive is still
# written at full session length, because that is what the coverage floor
# is measured against.
INDEX_BARS = 30


def seed(db, option_days=None):
    seed_index_rows(db, candles(DAYS, bars=INDEX_BARS))
    if option_days:
        seed_option_rows(db, option_days, EXPIRY, strikes=STRIKES)


def post(client, **overrides):
    body = {"symbol": "NIFTY", "timeframe": "5m",
            "pricing_policy": "modelled_only"} | overrides
    return client.post("/backtest/option-buying", json=body)


# ---- refusals -----------------------------------------------------------

def test_an_empty_archive_refuses_with_a_coverage_report(client, db):
    response = post(client)
    assert response.status_code == 409

    body = response.json()["detail"]
    assert body["error"] == "insufficient option coverage"
    assert "index archive" in body["reason"]
    assert body["fix"]


def test_observed_only_over_a_window_with_holes_refuses(client, db):
    seed(db, option_days=DAYS[:4])
    response = post(client, pricing_policy=OBSERVED_ONLY)
    assert response.status_code == 409

    body = response.json()["detail"]
    assert "2 of 6 sessions" in body["reason"]
    assert "cannot be backfilled" in body["fix"]
    # The report names them rather than only counting them.
    missing = [s for s in body["options"]["per_session"] if not s["covered"]]
    assert {s["session"] for s in missing} == {d.isoformat() for d in DAYS[4:]}


def test_the_refusal_does_not_leak_a_partial_result(client, db):
    """A 409 that also carried statistics would be read as a result with a
    warning attached, which is exactly the habit the gate exists to stop."""
    seed(db, option_days=DAYS[:4])
    body = post(client, pricing_policy=OBSERVED_ONLY).json()["detail"]
    assert "trades" not in body
    assert "stats" not in body


def test_a_mostly_modelled_run_is_refused_when_evidence_was_required(client, db):
    seed(db)
    response = post(client, pricing_policy=MODELLED_ONLY, min_observed_pct=50)
    assert response.status_code == 409

    body = response.json()["detail"]
    assert body["error"] == "insufficient observed pricing"
    assert body["evidence"]["required_observed_pct"] == 50.0
    # The dataset is still named, so the refusal itself is reproducible.
    assert body["dataset"]["index"]["hash"]


def test_an_unknown_policy_is_rejected(client, db):
    seed(db)
    assert post(client, pricing_policy="cheapest").status_code == 422


def test_an_impossible_tenor_window_is_rejected(client, db):
    seed(db)
    response = post(client, min_days_to_expiry=10, max_days_to_expiry=2)
    assert response.status_code == 422


# ---- a run that is allowed ---------------------------------------------

def test_a_modelled_run_completes_and_labels_everything(client, db):
    seed(db)
    response = post(client)
    assert response.status_code == 200, response.json()

    body = response.json()
    assert body["strategy"] == "option_buying"
    assert body["coverage"]["ok"] is True
    assert body["coverage"]["options"]["consulted"] is False
    assert body["dataset"]["index"]["hash"]
    assert body["assumptions"]["model"]["iv_source"] == "constant"
    assert all(t["evidence"] == MODELLED for t in body["trades"])
    assert any("constant IV" in line for line in body["limitations"])


def test_an_observed_run_names_the_option_archive_it_read(client, db):
    seed(db, option_days=DAYS)
    response = post(client, pricing_policy=OBSERVED_ONLY)
    assert response.status_code == 200, response.json()

    body = response.json()
    assert body["dataset"]["options"]["hash"]
    assert body["dataset"]["options"]["rows"] > 0
    assert body["dataset"]["options"]["bar_kinds"] == {"snapshot":
                                                      body["dataset"]["options"]["rows"]}
    assert body["coverage"]["options"]["sessions_covered"] == 6


def test_the_coverage_endpoint_answers_without_running_anything(client, db):
    """So the state of the archive can be checked before committing to a
    run, and so a refusal can be read on its own rather than as an error."""
    seed(db, option_days=DAYS[:4])
    response = client.get("/backtest/option-buying/coverage",
                          params={"pricing_policy": OBSERVED_ONLY})
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is False
    assert body["options"]["sessions_missing"] == 2
    assert "trades" not in body


def test_the_coverage_endpoint_rejects_an_unknown_policy(client, db):
    response = client.get("/backtest/option-buying/coverage",
                          params={"pricing_policy": "vibes"})
    assert response.status_code == 422


# ---- the runner, without HTTP -------------------------------------------

def test_the_runner_registers_both_fingerprints(db):
    """Two runs over identical candles can differ because the chain archive
    grew underneath them. Without the second hash, both results would name
    the same dataset."""
    from app.data import dataset as dataset_module

    seed(db, option_days=DAYS)
    request = runner.OptionBuyRequest(
        run=OptionBuyConfig(pricing_policy=OBSERVED_ONLY, warmup=20),
        selection=SelectionConfig(min_premium=0.5))
    runner.execute(db, request)

    registered = {row["symbol"] for row in dataset_module.recent(db)}
    assert "NIFTY" in registered
    assert "NIFTY:options" in registered


def test_the_runner_raises_a_refusal_carrying_its_report(db):
    seed(db, option_days=DAYS[:3])
    request = runner.OptionBuyRequest(
        run=OptionBuyConfig(pricing_policy=OBSERVED_ONLY))

    with pytest.raises(runner.CoverageRefused) as caught:
        runner.execute(db, request)

    assert caught.value.report.ok is False
    assert "3 of 6 sessions" in caught.value.report.reason


def test_the_gate_runs_before_the_walk(db, monkeypatch):
    """A refusal delivered after five minutes of walking invites reading the
    numbers anyway."""
    from app.optionbuy import strategy as strategy_module

    seed(db, option_days=DAYS[:2])
    called = []
    monkeypatch.setattr(strategy_module, "run",
                        lambda *a, **kw: called.append(1))

    with pytest.raises(runner.CoverageRefused):
        runner.execute(db, runner.OptionBuyRequest(
            run=OptionBuyConfig(pricing_policy=OBSERVED_ONLY)))
    assert called == []
