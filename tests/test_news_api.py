"""The news endpoint, its cache, and the wall around it.

`test_no_trading_module_imports_the_news_package` is the one that matters
most and is not really about the endpoint at all. A headline is context for
a person reading the screen; the moment it becomes an input to a rule, the
desk is running a strategy nobody backtested, on a lexicon nobody validated,
against a source that can go stale behind a healthy 200.

So the wall is asserted rather than assumed, and it fails the day somebody
wires sentiment into the signal engine by accident.
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import news as news_api  # noqa: E402

SAMPLE = {
    "items": [{"id": "a1", "headline": "Sensex surges to record high",
               "source": "Economic Times", "label": "markets",
               "published_at": "2026-08-25T13:40:00+00:00", "age_hours": 0.4,
               "sentiment": "positive", "sentiment_score": 1.0,
               "sentiment_reason": "Matched record high +2, surges +2."}],
    "sentiment": {"label": "positive", "score": 0.5,
                  "counts": {"positive": 1, "negative": 0, "neutral": 0},
                  "scored": 1, "total": 1, "note": "…"},
    "sources": [{"source": "Economic Times", "items": 50, "problem": None}],
    "fetched_at": "2026-08-25T13:45:00+00:00",
    "available": True,
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(news_api.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """Cache misses by default, and record what gets written."""
    written = {}
    monkeypatch.setattr(news_api, "get_json", lambda key: None)
    monkeypatch.setattr(news_api, "set_json",
                        lambda key, value, ttl=0: written.update(
                            {"key": key, "value": value, "ttl": ttl}))
    return written


def test_it_serves_headlines_with_their_tone(client, monkeypatch):
    monkeypatch.setattr(news_api.feeds, "collect", lambda: SAMPLE)
    body = client.get("/news").json()

    assert body["available"] is True
    assert body["items"][0]["headline"] == "Sensex surges to record high"
    assert body["items"][0]["sentiment"] == "positive"
    assert body["sentiment"]["counts"]["positive"] == 1


def test_every_item_carries_the_reason_for_its_tone(client, monkeypatch):
    monkeypatch.setattr(news_api.feeds, "collect", lambda: SAMPLE)
    item = client.get("/news").json()["items"][0]
    assert "record high" in item["sentiment_reason"]


def test_a_good_answer_is_cached(client, monkeypatch, no_redis):
    monkeypatch.setattr(news_api.feeds, "collect", lambda: SAMPLE)
    client.get("/news")
    assert no_redis["key"] == news_api.CACHE_KEY
    assert no_redis["ttl"] == news_api.CACHE_TTL_SECONDS


def test_an_empty_answer_is_not_cached(client, monkeypatch, no_redis):
    """Caching a dark panel would hold it dark for the whole TTL after one
    bad minute."""
    monkeypatch.setattr(news_api.feeds, "collect",
                        lambda: {**SAMPLE, "items": [], "available": False})
    client.get("/news")
    assert no_redis == {}


def test_a_cache_hit_says_so_and_makes_no_request(client, monkeypatch):
    called = []
    monkeypatch.setattr(news_api, "get_json", lambda key: SAMPLE)
    monkeypatch.setattr(news_api.feeds, "collect",
                        lambda: called.append(1) or SAMPLE)

    body = client.get("/news").json()
    assert body["cached"] is True
    assert called == []


def test_a_fetch_failure_answers_rather_than_erroring(client, monkeypatch):
    """The panel is context. It must never be the thing that breaks the
    page."""
    def boom():
        raise RuntimeError("every feed refused")
    monkeypatch.setattr(news_api.feeds, "collect", boom)

    response = client.get("/news")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["items"] == []
    assert "context only" in body["note"]


def test_the_failing_publishers_are_named(client, monkeypatch):
    """A dark panel that will not say what went wrong is indistinguishable
    from a quiet news day."""
    monkeypatch.setattr(news_api.feeds, "collect", lambda: {
        **SAMPLE, "items": [], "available": False,
        "sources": [{"source": "Livemint", "items": 0, "problem": "HTTP 503"}]})

    body = client.get("/news").json()
    assert body["sources"][0]["problem"] == "HTTP 503"


def test_the_endpoint_needs_no_api_key(client, monkeypatch):
    """Reads stay open on this desk; only writes are keyed."""
    monkeypatch.setattr(news_api.feeds, "collect", lambda: SAMPLE)
    assert client.get("/news").status_code == 200


# --------------------------------------------------------------- the wall

def test_no_trading_module_imports_the_news_package():
    """Sentiment is context, never an input.

    Checked by reading the source rather than by convention, because the
    convention is exactly what an accidental import would break — and it
    would break it silently, producing a desk whose signals depend on a word
    list nobody validated.
    """
    root = Path(__file__).resolve().parents[1] / "backend" / "app"
    guarded = ["analytics", "risk", "backtest", "evaluation", "data", "workers"]

    offenders = []
    for area in guarded:
        for path in (root / area).rglob("*.py"):
            source = path.read_text()
            if "news" in source and ("import news" in source
                                     or "from ..news" in source
                                     or "from .news" in source
                                     or "app.news" in source):
                offenders.append(str(path.relative_to(root)))

    assert offenders == [], f"news reached trading logic: {offenders}"


def test_the_aggregate_disclaims_itself_where_a_reader_will_see_it():
    from app.news import sentiment

    note = sentiment.aggregate([sentiment.score("Nifty rallies")])["note"]
    assert "not a view on NIFTY" in note
    assert "signal engine" in note
