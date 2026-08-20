"""Live market-data latency and staleness.

These tests exist because of a specific failure: the desk showed a price
that was up to a minute behind the market while reporting itself as three
seconds old. Everything below defends one of the two ideas that fixed it.

  1. A price carries the time the *market* printed it, not the time we
     fetched it. Only the first can measure staleness.
  2. The live price and the five-minute signal are independent. The price
     must keep moving between signals, and the signal must not.

Nothing here touches the network. The broker is a stub whose quote can be
frozen, delayed or broken on demand, which is the point — the failures
worth testing are the ones a live source produces rarely and at the worst
possible moment.
"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.workers import ticker
from app.workers.ticker import DELAYED_SECONDS, LIVE_SECONDS, classify_age


class FakeRedis:
    """Records publishes in order, with the moment each one happened."""

    def __init__(self):
        self.published: list[tuple[str, dict, datetime]] = []
        self.stored: dict[str, tuple[int, dict]] = {}

    def setex(self, key, ttl, blob):
        self.stored[key] = (ttl, json.loads(blob))

    def publish(self, channel, blob):
        self.published.append((channel, json.loads(blob), datetime.now(UTC)))


class StubBroker:
    """A quote whose age and availability are dictated by the test."""

    def __init__(self, price=24_200.0, age_seconds=2.0):
        self.price = price
        self.age_seconds = age_seconds
        self.source_time = None      # set to override the computed stamp
        self.fail = False
        self.calls = 0

    def quote(self, symbol="NIFTY"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("source unreachable")
        stamp = self.source_time
        if stamp is None and self.age_seconds is not None:
            stamp = (datetime.now(UTC)
                     - timedelta(seconds=self.age_seconds)).isoformat()
        return {"last_price": self.price, "source": "stub", "source_time": stamp}


@pytest.fixture
def rig(monkeypatch):
    """A ticker wired to a stub broker and a recording Redis."""
    broker = StubBroker()
    redis = FakeRedis()
    monkeypatch.setattr(ticker, "get_broker", lambda: broker)
    monkeypatch.setattr(ticker, "redis_client", lambda: redis)
    monkeypatch.setattr(ticker, "market_is_open", lambda: True)
    ticker._previous.clear()
    return broker, redis


def last_price_payload(redis):
    prices = [p for channel, p, _ in redis.published if channel == ticker.CHANNEL]
    assert prices, "the ticker published nothing to the price channel"
    return prices[-1]


# ---------------------------------------------------------------------------
# 1. live price update latency
# ---------------------------------------------------------------------------

def test_published_price_carries_the_market_clock_not_ours(rig):
    """The whole fix in one assertion.

    `at` is when we published; `source_time` is when the market printed it.
    The gap between them is the latency, and it is only measurable because
    the two are reported separately.
    """
    broker, redis = rig
    broker.age_seconds = 4.0

    ticker.tick()

    payload = last_price_payload(redis)
    assert payload["source_time"] is not None
    published = datetime.fromisoformat(payload["at"])
    printed = datetime.fromisoformat(payload["source_time"])
    assert published > printed
    # The ticker measured the same gap the test set up.
    assert payload["age_seconds"] == pytest.approx(4.0, abs=0.5)


def test_internal_latency_is_small(rig):
    """Our own contribution — fetch to publish — must stay negligible.

    This is the number that says whether a lag is ours or the source's. If
    this test ever starts failing, the bottleneck has moved inside
    QuantDesk and the fix is here rather than at the feed.
    """
    broker, redis = rig
    broker.age_seconds = 0.0

    before = datetime.now(UTC)
    ticker.tick()
    after = datetime.now(UTC)

    payload = last_price_payload(redis)
    published = datetime.fromisoformat(payload["at"])
    assert before <= published <= after
    assert (after - before).total_seconds() < 1.0


# ---------------------------------------------------------------------------
# 2. Redis publish immediately follows the ticker update
# ---------------------------------------------------------------------------

def test_every_tick_publishes_and_caches_together(rig):
    """No buffering, no batching: one poll, one publish, straight away."""
    broker, redis = rig

    ticker.tick()

    assert len(redis.published) == 1
    channel, payload, when = redis.published[0]
    assert channel == ticker.CHANNEL
    assert (datetime.now(UTC) - when).total_seconds() < 1.0

    # The cached copy a reconnecting browser reads must be the same object
    # that was pushed, or the two views of the desk disagree.
    ttl, cached = redis.stored[ticker.CACHE_KEY]
    assert cached == payload
    assert ttl > 0


def test_a_failed_poll_publishes_nothing_rather_than_a_stale_repeat(rig):
    """Silence beats a lie.

    Re-publishing the previous price with a fresh timestamp would reset
    every staleness indicator downstream and hide an outage completely.
    """
    broker, redis = rig
    ticker.tick()
    assert len(redis.published) == 1

    broker.fail = True
    ticker.tick()
    ticker.tick()

    assert len(redis.published) == 1


# ---------------------------------------------------------------------------
# 3. stale-data detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("age,expected", [
    (None, "unknown"),
    (0.0, "live"),
    (2.0, "live"),
    (LIVE_SECONDS, "live"),
    (LIVE_SECONDS + 0.1, "delayed"),
    (30.0, "delayed"),
    (DELAYED_SECONDS, "delayed"),
    (DELAYED_SECONDS + 0.1, "stale"),
    (134.0, "stale"),
])
def test_age_classification(age, expected):
    assert classify_age(age) == expected


def test_undated_price_is_never_called_live(rig):
    """A source that will not say when it printed a price gets no benefit
    of the doubt. `unknown` is the honest answer; `live` would be a guess
    that happens to look reassuring."""
    broker, redis = rig
    broker.age_seconds = None
    broker.source_time = None

    ticker.tick()

    payload = last_price_payload(redis)
    assert payload["source_time"] is None
    assert payload["age_seconds"] is None
    assert payload["freshness"] == "unknown"


def test_a_frozen_source_is_reported_stale(rig):
    """The exact failure that started this: the feed stops updating but the
    poll keeps succeeding, so everything looks healthy."""
    broker, redis = rig
    frozen = (datetime.now(UTC) - timedelta(minutes=2, seconds=14)).isoformat()
    broker.source_time = frozen

    ticker.tick()

    payload = last_price_payload(redis)
    assert payload["freshness"] == "stale"
    assert payload["age_seconds"] > 120


def test_age_is_computed_from_absolute_instants_across_timezones():
    """A price stamped in IST and read in UTC is the same instant.

    Comparing rendered clock strings instead would report a live feed as
    five and a half hours stale, or a stale one as live.
    """
    from app.brokers.nse import parse_nse_timestamp

    ist_stamp = parse_nse_timestamp("20-Aug-2026 10:34")
    assert ist_stamp == "2026-08-20T05:04:00+00:00"

    # Same instant, expressed two ways, must have zero age between them.
    as_utc = datetime.fromisoformat(ist_stamp)
    assert as_utc.utcoffset() == timedelta(0)
    assert (as_utc - datetime(2026, 8, 20, 5, 4, tzinfo=UTC)).total_seconds() == 0


@pytest.mark.parametrize("raw", [None, "", "not a date", "20-Foo-2026 10:34"])
def test_unparseable_source_timestamps_degrade_to_unknown(raw):
    """A malformed stamp must not crash the ticker or be silently coerced
    into `now`, which would mark unusable data as perfectly fresh."""
    from app.brokers.nse import parse_nse_timestamp
    assert parse_nse_timestamp(raw) is None
