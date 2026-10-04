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

from app.workers import prices, ticker
from app.workers.ticker import DELAYED_SECONDS, LIVE_SECONDS, classify_age


class FakeRedis:
    """Stands in for `cache.publish`, recording what was sent and when.

    Matches that function's signature rather than a raw Redis client: the
    ticker no longer talks to Redis directly, it goes through the helper
    that survives an outage (see cache.py and audit finding M-1).
    """

    def __init__(self):
        self.published: list[tuple[str, dict, datetime]] = []
        self.stored: dict[str, tuple[int, dict]] = {}

    def publish(self, channel, blob, cache_key=None, ttl=120):
        self.published.append((channel, json.loads(blob), datetime.now(UTC)))
        if cache_key:
            self.stored[cache_key] = (ttl, json.loads(blob))
        return True


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
    # The publish seam lives in `workers.prices` now: the Angel feed
    # and the poller share one publisher so their payloads cannot
    # drift apart.
    monkeypatch.setattr(prices, "publish", redis.publish)
    monkeypatch.setattr(ticker, "market_is_open", lambda: True)
    prices.reset_previous()
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


def test_a_second_resolution_source_never_reports_negative_latency():
    """Angel stamps `exchange_timestamp` to the whole second.

    A tick that leaves at 10:05:48.994 carries 10:05:49, so the naive
    subtraction makes it arrive six milliseconds before it was sent. A
    negative latency on the dashboard reads as a broken clock, and it drags
    the rolling p50 below anything achievable. Zero is the honest floor.
    """
    received = datetime(2026, 8, 26, 10, 5, 48, 993661, tzinfo=UTC)
    stamped = datetime(2026, 8, 26, 10, 5, 49, tzinfo=UTC)          # rounded up

    payload = prices.build_payload(
        "NIFTY", 24207.75, source="angel", source_time=stamped.isoformat(),
        received_at=received, transport="stream")

    assert payload["feed_latency_ms"] == 0.0, (
        "a source stamped to the second must floor at zero, not go negative")


# ---------------------------------------------------------------------------
# 4. the resolution of the source clock, and what may be derived from it
# ---------------------------------------------------------------------------
#
# The test above establishes that Angel's stamp is rounded to the second.
# What it did not say is what follows from that, which cost the desk three
# separate false latency alarms on 15-Sep-2026: every quantity derived from
# `source_time` carries up to a second of rounding, and a dashboard that
# renders an age to the nearest second is therefore rendering almost pure
# rounding. The pill alternated "just now" / "1s ago" on consecutive seconds
# while the feed sat at 97ms between ticks with zero reconnects.
#
# Measured that morning over 167 consecutive ticks: no `source_time` carried
# a sub-second digit, and `feed_latency_ms` was spread dead flat across every
# 100ms bucket from 0 to 1000. Real transit clusters; a flat band exactly one
# second wide is a rounded clock. p50 read 562ms, the floor 56ms.

def test_a_whole_second_stamp_is_reported_as_second_resolution():
    """The uncertainty is published rather than left to be rediscovered."""
    stamped = datetime(2026, 9, 15, 9, 11, 3, tzinfo=UTC)
    received = datetime(2026, 9, 15, 9, 11, 3, 757_647, tzinfo=UTC)

    payload = prices.build_payload(
        "NIFTY", 23_217.6, source="angel", source_time=stamped.isoformat(),
        received_at=received, transport="stream")

    assert payload["source_time_quantum_ms"] == 1000.0, (
        "a stamp with no sub-second digits cannot date itself more finely "
        "than a second, and the payload has to say so — the 757ms latency "
        "beside it is mostly that rounding")


def test_a_finely_stamped_source_carries_no_such_caveat():
    """The correction must retire itself the day the vendor improves.

    Hard-coding "Angel is coarse" would still be claiming it long after it
    stopped being true, and would quietly libel every other source that
    dates itself properly.
    """
    stamped = datetime(2026, 9, 15, 9, 11, 3, 412_000, tzinfo=UTC)
    received = datetime(2026, 9, 15, 9, 11, 3, 470_000, tzinfo=UTC)

    payload = prices.build_payload(
        "NIFTY", 23_217.6, source="angel", source_time=stamped.isoformat(),
        received_at=received, transport="stream")

    assert payload["source_time_quantum_ms"] == 0.0
    assert payload["feed_latency_ms"] == pytest.approx(58, abs=1)


def test_the_poller_stamps_when_it_received_the_quote(rig):
    """Both sources must publish the same shape, including this field.

    `prices.publish_price` exists so the push feed and the poller cannot
    drift apart, but the poller was never passing `received_at` — so the
    push feed published four timestamps and the poller three. That was
    invisible until the dashboard started measuring freshness from
    `received_at` to escape the rounding above. Without this, a fallback to
    the poller would have silently dropped the dashboard back to ageing
    against the coarse stamp, and the flicker would have returned wearing a
    different hat, only during an outage, which is the worst time to be
    debugging a clock.
    """
    broker, redis = rig
    broker.age_seconds = 2.0

    before = datetime.now(UTC)
    ticker.tick()
    after = datetime.now(UTC)

    payload = last_price_payload(redis)
    assert payload["received_at"] is not None, (
        "the poller must stamp when the quote landed, as the push feed does")

    received = datetime.fromisoformat(payload["received_at"])
    assert before <= received <= after

    # And it must be the arrival, not the print: the stub's quote is two
    # seconds old, so these two cannot be the same instant.
    printed = datetime.fromisoformat(payload["source_time"])
    assert (received - printed).total_seconds() == pytest.approx(2.0, abs=0.5)


def test_every_published_price_carries_the_four_timestamps(rig):
    """The shape itself, asserted once, so a fifth caller cannot omit one."""
    broker, redis = rig
    broker.age_seconds = 1.0
    ticker.tick()

    payload = last_price_payload(redis)
    for field in ("source_time", "received_at", "at",
                  "source_time_quantum_ms"):
        assert field in payload, f"{field} missing from the published price"
