"""Two sources, one payload shape, and an explicit answer to "which one?".

The failure this guards against is not dramatic. It is the desk showing a
price that came from somewhere other than it thinks, or two sources racing
into the same Redis key and taking turns — the change column flipping
between two slightly different numbers with the `source` field as the only
clue.
"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings
from app.workers import angel_feed, prices, ticker

MOMENT = datetime(2026, 8, 26, 4, 45, tzinfo=UTC)     # 10:15 IST


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    prices.reset_previous()
    angel_feed.FEED = angel_feed.AngelFeed()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Recorder:
    """Captures what would have gone to Redis."""

    def __init__(self):
        self.published = []

    def __call__(self, channel, blob, cache_key=None, ttl=120):
        self.published.append((channel, json.loads(blob), cache_key, ttl))
        return True


@pytest.fixture
def redis(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(prices, "publish", recorder)
    return recorder


def a_quote(price=24_300.0, source="yahoo", age=2.0):
    return {"last_price": price, "source": source,
            "source_time": (MOMENT - timedelta(seconds=age)).isoformat()}


def make_feed_healthy(price=24_334.55):
    feed = angel_feed.FEED
    feed.stats.last_tick_at = datetime.now(UTC)
    feed.stats.last_source_time = datetime.now(UTC)
    feed.stats.last_price = price
    return feed


# ---- the poller stands down when the feed is serving ---------------------

def test_the_ticker_does_not_poll_while_angel_is_healthy(monkeypatch, redis):
    """A poll cannot be fresher than a push. Running both would spend
    requests to produce a strictly worse number while racing it into the
    same Redis key."""
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    get_settings.cache_clear()
    make_feed_healthy()

    called = []
    monkeypatch.setattr(ticker, "get_broker",
                        lambda: called.append(1) or _never_called())

    ticker.tick()

    assert called == []
    assert redis.published == []


def test_the_ticker_polls_when_angel_has_gone_quiet(monkeypatch, redis):
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    get_settings.cache_clear()
    feed = make_feed_healthy()
    feed.stats.last_tick_at = datetime.now(UTC) - timedelta(seconds=30)

    monkeypatch.setattr(ticker, "get_broker", lambda: _broker(a_quote()))
    ticker.tick()

    assert len(redis.published) == 1
    channel, payload, cache_key, _ = redis.published[0]
    assert channel == "prices"
    assert cache_key == "price:latest"
    assert payload["source"] == "yahoo"
    assert payload["transport"] == "poll"


def test_falling_back_is_recorded_rather_than_smoothed_over(monkeypatch, redis):
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    get_settings.cache_clear()
    feed = make_feed_healthy()
    feed._was_healthy = True
    feed.stats.last_tick_at = datetime.now(UTC) - timedelta(seconds=30)

    monkeypatch.setattr(ticker, "get_broker", lambda: _broker(a_quote()))
    ticker.tick()

    assert feed.stats.fallbacks == 1
    assert feed.stats.stale_events == 1


def test_the_ticker_polls_normally_when_angel_is_switched_off(monkeypatch, redis):
    monkeypatch.setenv("ANGEL_ENABLED", "false")
    get_settings.cache_clear()

    monkeypatch.setattr(ticker, "get_broker", lambda: _broker(a_quote()))
    ticker.tick()

    assert len(redis.published) == 1
    assert redis.published[0][1]["source"] == "yahoo"
    # Nothing to fall back *from*, so nothing is counted as a fallback.
    assert angel_feed.FEED.stats.fallbacks == 0


# ---- one payload shape ----------------------------------------------------

def test_both_sources_publish_the_same_keys(monkeypatch, redis):
    """If the shapes differ, the dashboard reads one correctly and the other
    approximately, and the day the feed falls back is the day the desk
    quietly starts displaying something different."""
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    get_settings.cache_clear()

    feed = angel_feed.AngelFeed(clock=lambda: MOMENT)
    feed._on_data(None, {"token": "99926000", "last_traded_price": 2433455,
                         "exchange_timestamp": int(MOMENT.timestamp() * 1000)})
    from_stream = redis.published[-1][1]

    prices.reset_previous()
    monkeypatch.setattr(ticker, "get_broker", lambda: _broker(a_quote()))
    monkeypatch.setattr(angel_feed, "healthy", lambda: False)
    ticker.tick()
    from_poll = redis.published[-1][1]

    assert set(from_stream) == set(from_poll)
    assert from_stream["transport"] == "stream"
    assert from_poll["transport"] == "poll"


def test_the_change_column_survives_a_failover(monkeypatch, redis):
    """Resetting the previous price on failover would read as the market
    having returned to its open."""
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    get_settings.cache_clear()

    feed = angel_feed.AngelFeed(clock=lambda: MOMENT)
    feed._on_data(None, {"token": "99926000", "last_traded_price": 2433455,
                         "exchange_timestamp": int(MOMENT.timestamp() * 1000)})

    monkeypatch.setattr(angel_feed, "healthy", lambda: False)
    monkeypatch.setattr(ticker, "get_broker",
                        lambda: _broker(a_quote(price=24_340.55)))
    ticker.tick()

    polled = redis.published[-1][1]
    assert polled["previous"] == 24_334.55
    assert polled["change"] == pytest.approx(6.0)
    assert polled["direction"] == "up"


# ---- Redis publishing ------------------------------------------------------

def test_a_tick_reaches_the_prices_channel_and_the_cache_key(redis):
    feed = angel_feed.AngelFeed(clock=lambda: MOMENT)
    feed._on_data(None, {"token": "99926000", "last_traded_price": 2433455,
                         "exchange_timestamp": int(MOMENT.timestamp() * 1000)})

    channel, payload, cache_key, ttl = redis.published[0]
    assert channel == prices.CHANNEL == "prices"
    assert cache_key == prices.CACHE_KEY == "price:latest"
    assert ttl == 120
    assert payload["price"] == 24_334.55


def test_a_redis_outage_costs_one_tick_and_nothing_more(monkeypatch):
    """A publish failure must not kill the reader thread, or one Redis blip
    ends the session's price feed."""
    def explode(*a, **kw):
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(prices, "publish", explode)
    feed = angel_feed.AngelFeed(clock=lambda: MOMENT)

    feed._on_data(None, {"token": "99926000", "last_traded_price": 2433455,
                         "exchange_timestamp": int(MOMENT.timestamp() * 1000)})

    assert feed.stats.ticks == 1
    assert feed.stats.published == 0
    assert "redis unreachable" in feed.stats.last_error


def _broker(quote):
    class Stub:
        name = "stub"

        def quote(self, symbol):
            return quote
    return Stub()


def _never_called():
    raise AssertionError("the ticker polled while the Angel feed was healthy")
