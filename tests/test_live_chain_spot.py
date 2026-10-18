"""The streamed chain must be read against the market, not against its band.

`live_chain` summarised the chain against the median strike of the band it
was subscribed to. The band is centred on the spot when built and only
re-centred after a 250-point drift, so the summary's "spot" could sit that
far from NIFTY. On 31-Aug the desk showed ATM 24,150 with NIFTY at 24,022.
`summarise` scores spot against max pain, so the chain's bias could flip on
that error alone.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import market as market_api
from app.workers import option_chain_live


def band(centre: float, half: int = 20, step: float = 50.0) -> pd.DataFrame:
    """A chain band like the live universe: strikes around `centre`, with
    heavy OI at 24,100 so max pain lands there."""
    rows = []
    for i in range(-half, half):
        k = centre + i * step
        heavy = 50_000.0 if k == 24_100 else 1_000.0
        rows.append({"strike": k, "call_oi": heavy, "put_oi": heavy,
                     "call_ltp": 10.0, "put_ltp": 10.0,
                     "call_volume": 1.0, "put_volume": 1.0,
                     "call_bid": 9.9, "call_ask": 10.1,
                     "put_bid": 9.9, "put_ask": 10.1})
    return pd.DataFrame(rows)


@pytest.fixture
def streamed(monkeypatch):
    """A streaming chain centred on 24,150, as on 31-Aug."""
    monkeypatch.setenv("ANGEL_OPTIONS_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    frame = band(24_150)
    snap = {"frame": frame, "at": "2026-08-31T04:28:00+00:00",
            "expiry": "2026-09-01", "oldest_age_seconds": 0.4,
            "contracts": 80, "dropped_stale": 0}
    monkeypatch.setattr(option_chain_live.CHAIN, "snapshot", lambda **_: snap)
    yield
    get_settings.cache_clear()


def with_price(monkeypatch, price):
    monkeypatch.setattr(market_api, "get_json",
                        lambda key: {"price": price} if key == "price:latest" else None)


def test_option_only_outage_uses_http_fallback(streamed, monkeypatch):
    snapshot = option_chain_live.CHAIN.snapshot()
    snapshot["newest_age_seconds"] = 16.0
    cached = {"transport": "poll", "symbol": "NIFTY", "strikes": []}
    monkeypatch.setattr(market_api, "get_json", lambda key: cached)
    assert market_api.live_chain() is None
    assert market_api.option_chain("NIFTY") == cached


def test_quiet_wings_do_not_hide_a_chain_with_fresh_quotes(streamed, monkeypatch):
    snapshot = option_chain_live.CHAIN.snapshot()
    snapshot.update(newest_age_seconds=0.2, oldest_age_seconds=600.0)
    with_price(monkeypatch, 24000)
    assert market_api.live_chain()["transport"] == "stream"


def test_the_summary_uses_the_live_spot_not_the_band_centre(streamed, monkeypatch):
    with_price(monkeypatch, 24_022.75)
    summary = market_api.live_chain()["summary"]
    assert summary["spot"] == pytest.approx(24_022.75)
    assert summary["atm_strike"] == 24_000


def test_the_atm_follows_the_market_through_the_band(streamed, monkeypatch):
    """The band stays put until a 250-point drift; ATM must not."""
    with_price(monkeypatch, 24_310.0)
    assert market_api.live_chain()["summary"]["atm_strike"] == 24_300


def test_the_bias_is_read_against_the_real_spot(streamed, monkeypatch):
    """Max pain is 24,100. Spot below it scores bullish, above it bearish —
    so a band centre of 24,150 and a market at 24,022 read opposite ways."""
    with_price(monkeypatch, 24_022.75)
    below = market_api.live_chain()["summary"]
    with_price(monkeypatch, 24_150.0)
    above = market_api.live_chain()["summary"]
    assert below["max_pain"] == above["max_pain"] == 24_100
    assert below["max_pain_distance_pct"] < 0 < above["max_pain_distance_pct"]


@pytest.mark.parametrize("latest", [None, {}, {"price": None}, {"price": 0},
                                    {"price": "garbage"}])
def test_it_falls_back_to_the_band_when_no_price_has_published(
        streamed, monkeypatch, latest):
    monkeypatch.setattr(market_api, "get_json", lambda key: latest)
    summary = market_api.live_chain()["summary"]
    assert summary["spot"] == pytest.approx(float(band(24_150)["strike"].median()))
