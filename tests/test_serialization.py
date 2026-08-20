"""Regression tests for JSON-safe DataFrame serialisation.

`/market/candles` returned 500 on every single request against the free
data path. Relative volume is NaN for the whole series when the source
publishes no volume — which for Yahoo's Indian index tickers is always — and
NaN has no JSON representation, so FastAPI refused to encode the response.

The endpoint was not intermittently broken. It was permanently broken, and
the data behind it was fine the whole time.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import indicators
from app.api.serialization import has_unserialisable_floats, jsonable_records
from app.brokers.mock import MockBroker

# ---- the helper -------------------------------------------------------

def test_nan_becomes_null_not_a_substituted_number():
    """Null means "no reading". A substituted 0 or 1 would be
    indistinguishable from a real neutral reading, which is worse than
    missing data because it cannot be detected."""
    df = pd.DataFrame({"a": [1.0, np.nan, 3.0]})
    records = jsonable_records(df)
    assert records[1]["a"] is None
    assert records[0]["a"] == 1.0


def test_infinity_becomes_null():
    """Infinity only arises from a division that should not have happened,
    and a client has no more use for it than for NaN."""
    df = pd.DataFrame({"a": [np.inf, -np.inf, 2.0]})
    records = jsonable_records(df)
    assert records[0]["a"] is None
    assert records[1]["a"] is None
    assert records[2]["a"] == 2.0


def test_timestamps_are_stringified():
    df = pd.DataFrame({"timestamp": pd.to_datetime(["2026-08-17 04:00"], utc=True)})
    assert isinstance(jsonable_records(df)[0]["timestamp"], str)


def test_an_empty_frame_serialises_to_an_empty_list():
    assert jsonable_records(pd.DataFrame()) == []
    assert jsonable_records(None) == []


def test_real_values_are_not_altered():
    """A guard that rounds or coerces good data would be worse than the
    bug it replaced."""
    df = pd.DataFrame({"close": [24_001.25, 24_002.5], "volume": [1000, 2000]})
    records = jsonable_records(df)
    assert records[0]["close"] == 24_001.25
    assert records[1]["volume"] == 2000


def test_the_detector_itself_works():
    """Guard against the guard: a helper that never reports a problem would
    make every test below pass vacuously."""
    assert has_unserialisable_floats([{"a": float("nan")}])
    assert has_unserialisable_floats([{"a": float("inf")}])
    assert not has_unserialisable_floats([{"a": 1.0, "b": None, "c": "x"}])


# ---- the actual failure -----------------------------------------------

def test_an_all_nan_indicator_column_survives_serialisation():
    """The exact shape that broke the endpoint: constant volume makes
    `rvol` NaN for the entire series."""
    df = MockBroker(seed=5).candles(days=3, interval="5m")
    df["volume"] = 1.0                       # what the free adapter substitutes

    enriched = indicators.enrich(df)
    assert enriched["rvol"].isna().all(), "test premise: rvol should be all-NaN here"

    records = jsonable_records(enriched)
    assert not has_unserialisable_floats(records)
    assert all(r["rvol"] is None for r in records)


def test_warmup_nans_survive_serialisation():
    """Rolling indicators are NaN for their first bars whatever the source
    does. Those must serialise too, not only the all-NaN case."""
    df = MockBroker(seed=6).candles(days=2, interval="5m")
    records = jsonable_records(indicators.enrich(df))
    assert not has_unserialisable_floats(records)


# ---- the endpoint -----------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """A client whose broker returns candles with no real volume — the free
    data path's permanent condition."""
    from app.api import market

    class VolumelessBroker(MockBroker):
        def candles(self, symbol="NIFTY", interval="5m", days=5):
            df = super().candles(symbol, interval, days)
            df["volume"] = 1.0
            return df

    monkeypatch.setattr(market, "get_broker", lambda: VolumelessBroker())
    app = FastAPI()
    app.include_router(market.router)
    return TestClient(app)


def test_market_candles_returns_200_when_volume_is_synthetic(client):
    """The regression. This endpoint returned 500 on every request."""
    response = client.get("/market/candles?symbol=NIFTY&interval=5m&days=2")
    assert response.status_code == 200

    body = response.json()
    assert body["candles"], "no candles returned"
    assert not has_unserialisable_floats(body["candles"])


def test_market_candles_reports_unavailable_rvol_as_null(client):
    """Explicitly the behaviour asked for: unavailable, not zero."""
    body = client.get("/market/candles?symbol=NIFTY&interval=5m&days=2").json()
    assert all(row["rvol"] is None for row in body["candles"])
    # and the prices are still real
    assert all(isinstance(row["close"], (int, float)) for row in body["candles"])


def test_market_candles_response_is_strict_json(client):
    """`json.loads` with parse_constant rejects NaN and Infinity, which the
    default parser would otherwise accept — a response can be invalid JSON
    and still round-trip through Python without complaint."""
    import json

    raw = client.get("/market/candles?symbol=NIFTY&interval=5m&days=2").text

    def reject(constant):
        raise AssertionError(f"response contains {constant}, which is not valid JSON")

    json.loads(raw, parse_constant=reject)
