"""Redis connection recovery.

Audit finding M-1: the module latched `False` into its client global on the
first failure and checked `if _client is None`, so caching was disabled for
the lifetime of the process. A thirty-second Redis restart cost the rest of
the trading session, and the only trace was one log line.

These tests drive the clock rather than sleeping, so the backoff schedule is
asserted exactly instead of approximately.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import cache


class FakeRedis:
    """A Redis whose reachability the test controls."""

    def __init__(self, alive=True):
        self.alive = alive
        self.store, self.published, self.pings = {}, [], 0

    def _check(self):
        if not self.alive:
            raise ConnectionError("connection refused")

    def ping(self):
        self.pings += 1
        self._check()
        return True

    def get(self, key):
        self._check()
        return self.store.get(key)

    def setex(self, key, ttl, blob):
        self._check()
        self.store[key] = blob

    def publish(self, channel, blob):
        self._check()
        self.published.append((channel, blob))


@pytest.fixture
def rig(monkeypatch):
    """A controllable clock and a controllable Redis."""
    cache.reset()
    now = {"t": 1000.0}
    monkeypatch.setattr(cache.time, "monotonic", lambda: now["t"])

    server = FakeRedis()
    attempts = {"n": 0}

    def from_url(*a, **k):
        attempts["n"] += 1
        if not server.alive:
            raise ConnectionError("connection refused")
        return server

    fake_redis_module = type("m", (), {"from_url": staticmethod(from_url)})
    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)
    yield now, server, attempts
    cache.reset()


# ---------------------------------------------------------------------------
# the regression
# ---------------------------------------------------------------------------

def test_an_outage_no_longer_disables_caching_forever(rig):
    """The exact failure from the audit, now recovering on its own."""
    now, server, _ = rig

    server.alive = False
    assert cache.client() is None

    server.alive = True
    now["t"] += cache.RETRY_MIN_SECONDS          # wait out the first backoff
    assert cache.client() is server              # recovered, no restart


def test_retries_are_not_attempted_inside_the_backoff_window(rig):
    """A dead Redis must not be dialled once per tick."""
    now, server, attempts = rig
    server.alive = False

    cache.client()
    first = attempts["n"]
    for _ in range(20):
        assert cache.client() is None
    assert attempts["n"] == first                # nothing extra was tried

    now["t"] += cache.RETRY_MIN_SECONDS
    cache.client()
    assert attempts["n"] == first + 1            # exactly one retry after the wait


def test_backoff_doubles_and_is_bounded(rig):
    now, server, _ = rig
    server.alive = False

    seen = []
    for _ in range(12):
        cache.client()
        seen.append(cache._backoff)
        now["t"] += cache._backoff

    assert seen[0] == cache.RETRY_MIN_SECONDS
    assert seen[1] == cache.RETRY_MIN_SECONDS * 2
    assert seen[2] == cache.RETRY_MIN_SECONDS * 4
    assert max(seen) == cache.RETRY_MAX_SECONDS  # capped, never unbounded
    assert seen == sorted(seen)                  # monotonically increasing


def test_backoff_resets_after_a_successful_reconnect(rig):
    now, server, _ = rig
    server.alive = False
    for _ in range(4):
        cache.client()
        now["t"] += cache._backoff
    assert cache._backoff > cache.RETRY_MIN_SECONDS

    server.alive = True
    assert cache.client() is server
    assert cache._backoff == 0.0                 # a later blip waits 1s, not 8


# ---------------------------------------------------------------------------
# losing a live connection
# ---------------------------------------------------------------------------

def test_a_command_failing_on_a_live_handle_drops_it(rig):
    """Redis dying *after* connecting used to leave a handle that raised on
    every call, with nothing to rebuild it."""
    now, server, _ = rig
    assert cache.client() is server

    server.alive = False
    assert cache.get_json("price:latest") is None    # does not raise
    assert cache._client is None                     # handle discarded

    server.alive = True
    now["t"] += cache.RETRY_MIN_SECONDS
    assert cache.client() is server                  # back on its own


def test_set_json_survives_an_outage(rig):
    now, server, _ = rig
    cache.client()
    server.alive = False
    cache.set_json("k", {"a": 1})                    # must not raise
    assert cache._client is None


def test_publish_reports_failure_instead_of_raising(rig):
    """The ticker calls this on a scheduler thread. An exception here used to
    mean the job's remaining work was skipped."""
    now, server, _ = rig
    assert cache.publish("prices", '{"price": 1}', cache_key="price:latest") is True
    assert server.published == [("prices", '{"price": 1}')]
    assert server.store["price:latest"] == '{"price": 1}'

    server.alive = False
    assert cache.publish("prices", '{"price": 2}') is False   # no exception

    server.alive = True
    now["t"] += cache.RETRY_MIN_SECONDS
    assert cache.publish("prices", '{"price": 3}') is True


def test_publish_with_no_redis_at_all_is_a_quiet_false(rig):
    now, server, _ = rig
    server.alive = False
    assert cache.publish("prices", "{}") is False


# ---------------------------------------------------------------------------
# the ticker keeps ticking through an outage
# ---------------------------------------------------------------------------

def test_the_price_ticker_survives_and_resumes(rig, monkeypatch):
    now, server, _ = rig
    from app.workers import prices, ticker

    class Broker:
        def quote(self, symbol="NIFTY"):
            from datetime import UTC, datetime
            return {"last_price": 24_200.0, "source": "stub",
                    "source_time": datetime.now(UTC).isoformat()}

    monkeypatch.setattr(ticker, "get_broker", Broker)
    monkeypatch.setattr(ticker, "market_is_open", lambda: True)
    prices.reset_previous()

    ticker.tick()
    assert len(server.published) == 1

    server.alive = False
    for _ in range(3):
        ticker.tick()                                # must not raise
    assert len(server.published) == 1

    server.alive = True
    now["t"] += cache.RETRY_MAX_SECONDS
    ticker.tick()
    assert len(server.published) == 2                # publishing resumed
