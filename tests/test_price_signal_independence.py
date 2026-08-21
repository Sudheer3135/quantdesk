"""The price and the signal are two different clocks.

The bug this guards against is subtle and was the user-visible symptom: a
signal generated at 10:20 sat next to a price, and the price appeared to be
"the 10:20 price" because a single shared timestamp described both. At 10:24
the desk still read 10:20 — not because the feed was down, but because the
analysis was four minutes old and the interface could not say so.

So: the live price must move between signals, the signal must not move
between its own cycles, and neither may borrow the other's timestamp.

The strategy's cadence is not under test here and must not change. What is
under test is that a five-minute decision stays a five-minute decision.
"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.workers import ticker
from test_live_latency import FakeRedis, StubBroker


@pytest.fixture
def rig(monkeypatch):
    broker = StubBroker()
    redis = FakeRedis()
    monkeypatch.setattr(ticker, "get_broker", lambda: broker)
    monkeypatch.setattr(ticker, "publish", redis.publish)
    monkeypatch.setattr(ticker, "market_is_open", lambda: True)
    ticker._previous.clear()
    return broker, redis


def price_frames(redis):
    return [p for channel, p, _ in redis.published if channel == ticker.CHANNEL]


# ---------------------------------------------------------------------------
# the acceptance test from the brief
# ---------------------------------------------------------------------------

def test_price_advances_while_the_signal_timestamp_stands_still(rig):
    """10:24:10 a price arrives; at 10:24:30 the desk must not still be
    showing the 10:20 market price merely because the last signal was 10:20.

    The signal here is a fixed record, exactly as the agent left it. Only
    the ticker runs. If the two were coupled, the price would be pinned to
    the signal's timestamp and this would fail.
    """
    broker, redis = rig

    signal_at = datetime(2026, 8, 20, 10, 20, tzinfo=UTC)
    signal = {"timestamp": signal_at.isoformat(), "action": "BUY",
              "price": 24_200.0, "confidence": 0.61}

    # Three ticks across the four minutes after the signal.
    for offset, price in ((4 * 60 + 10, 24_223.35),
                          (4 * 60 + 20, 24_225.10),
                          (4 * 60 + 30, 24_219.85)):
        broker.price = price
        broker.source_time = (signal_at + timedelta(seconds=offset)).isoformat()
        ticker.tick()

    frames = price_frames(redis)
    assert [f["price"] for f in frames] == [24_223.35, 24_225.10, 24_219.85]

    # Every published price is stamped after the signal, and none of them
    # carries the signal's timestamp.
    for frame in frames:
        printed = datetime.fromisoformat(frame["source_time"])
        assert printed > signal_at
        assert frame["source_time"] != signal["timestamp"]

    # The signal record was never touched by any of it.
    assert signal["timestamp"] == signal_at.isoformat()
    assert signal["price"] == 24_200.0

    # And the desk can tell you both numbers, which is the whole point.
    assert frames[-1]["price"] != signal["price"]


def test_signal_timestamp_is_unchanged_between_signal_cycles(rig):
    """A signal is a decision taken at a moment. Re-reading it later, or
    ticking the price forty times underneath it, does not move that moment."""
    broker, redis = rig
    signal = {"timestamp": "2026-08-20T10:20:00+00:00", "action": "BUY"}
    original = json.dumps(signal, sort_keys=True)

    for i in range(40):
        broker.price = 24_200 + i * 0.25
        broker.source_time = (datetime(2026, 8, 20, 10, 20, tzinfo=UTC)
                              + timedelta(seconds=5 * i)).isoformat()
        ticker.tick()

    assert json.dumps(signal, sort_keys=True) == original
    assert len(price_frames(redis)) == 40


def test_the_ticker_cannot_produce_a_signal(rig):
    """Structural, not behavioural: the price loop publishes on the price
    channel only. If a refactor ever let it emit a signal, the strategy
    would silently gain a second cadence — a five-minute decision becoming
    a five-second one is exactly the failure the brief rules out."""
    broker, redis = rig

    for _ in range(12):
        ticker.tick()

    channels = {channel for channel, _, _ in redis.published}
    assert channels == {ticker.CHANNEL}
    assert "signals" not in channels

    keys = set(redis.stored)
    assert keys == {ticker.CACHE_KEY}
    assert "signal:latest" not in keys


def test_price_frames_carry_no_trading_decision(rig):
    """A price frame is data, not a verdict. Nothing downstream should be
    able to mistake one for the other."""
    broker, redis = rig
    ticker.tick()

    frame = price_frames(redis)[0]
    for forbidden in ("action", "confidence", "entry", "stop_loss",
                      "target", "checks", "risk"):
        assert forbidden not in frame


def test_the_ticker_never_reads_or_writes_the_signal_engine(monkeypatch, rig):
    """No look-ahead can be introduced through the price path, because the
    price path does not reach the strategy at all.

    Enforced by making any call into the engine fail loudly.
    """
    import app.analytics.signal_engine as engine

    def forbidden(*args, **kwargs):
        raise AssertionError("the price ticker called the signal engine")

    monkeypatch.setattr(engine, "generate", forbidden)

    for _ in range(5):
        ticker.tick()

    assert len(price_frames(rig[1])) == 5


def test_a_faster_ticker_does_not_change_signal_cadence(rig):
    """Raising the poll rate is a data-freshness change and nothing else.

    Sixty price polls produce sixty price frames and zero signals. This is
    the guard that lets the interval be tuned without anyone having to
    re-reason about the strategy.
    """
    broker, redis = rig

    for i in range(60):
        broker.price = 24_200 + i * 0.1
        broker.source_time = (datetime(2026, 8, 20, 10, 20, tzinfo=UTC)
                              + timedelta(seconds=i)).isoformat()
        ticker.tick()

    assert len(price_frames(redis)) == 60
    assert not [c for c, _, _ in redis.published if c == "signals"]
