"""The option chain reaching the browser as a push, not a poll.

The chain arrived over Angel's websocket and then waited for a browser to
ask for it. These cover the thread that closes that gap, and in particular
the three ways it could close it badly: republishing chains nobody changed,
publishing an empty chain during warm-up, and dying quietly so the panel
freezes while the feed underneath still reports healthy.
"""
from __future__ import annotations

import json

import pytest
from app.workers import chain_publisher
from app.workers.chain_publisher import CHAIN_CHANNEL, ChainPublisher


@pytest.fixture
def sent(monkeypatch):
    """Capture what reaches Redis instead of reaching Redis."""
    calls = []

    def fake_publish(channel, blob, cache_key=None, ttl=None):
        calls.append({"channel": channel, "payload": json.loads(blob),
                      "cache_key": cache_key, "ttl": ttl})
        return True

    monkeypatch.setattr(chain_publisher, "publish", fake_publish)
    return calls


@pytest.fixture
def chain_payload(monkeypatch):
    """Stand in for `live_chain()`, which is imported inside the method."""
    state = {"payload": {"symbol": "NIFTY", "transport": "stream",
                         "strikes": [{"strike": 24200.0}], "live": True}}

    import app.api.market as market
    monkeypatch.setattr(market, "live_chain", lambda: state["payload"])
    return state


@pytest.fixture
def version(monkeypatch):
    """Drive the tick counter the publisher uses to detect new content."""
    state = {"n": 1}
    monkeypatch.setattr(ChainPublisher, "_version",
                        staticmethod(lambda: state["n"]))
    return state


def test_publishes_the_streamed_chain(sent, chain_payload, version):
    assert ChainPublisher().publish_once() is True
    assert len(sent) == 1
    assert sent[0]["channel"] == CHAIN_CHANNEL
    assert sent[0]["payload"]["transport"] == "stream"


def test_caches_so_a_reconnecting_browser_is_not_left_blank(
        sent, chain_payload, version):
    ChainPublisher().publish_once()
    assert sent[0]["cache_key"] == "chain:stream:latest"
    assert sent[0]["ttl"] == chain_publisher.CACHE_TTL_SECONDS


def test_an_unchanged_chain_is_not_republished(sent, chain_payload, version):
    """Eighty contracts printing must not become eighty redraws a second."""
    pub = ChainPublisher()
    assert pub.publish_once() is True
    assert pub.publish_once() is False
    assert pub.publish_once() is False
    assert len(sent) == 1
    assert pub.skipped_unchanged == 2


def test_a_new_tick_publishes_again(sent, chain_payload, version):
    pub = ChainPublisher()
    pub.publish_once()
    version["n"] = 2
    assert pub.publish_once() is True
    assert len(sent) == 2


def test_nothing_is_published_while_the_chain_is_still_filling(
        sent, chain_payload, version):
    """`live_chain` returns None until both sides of three strikes quote.

    Publishing an empty chain here would blank a panel that the HTTP
    fallback is still filling correctly.
    """
    chain_payload["payload"] = None
    assert ChainPublisher().publish_once() is False
    assert sent == []


def test_a_warm_up_miss_does_not_mark_the_version_published(
        sent, chain_payload, version):
    """The first real chain must still go out after an empty warm-up."""
    pub = ChainPublisher()
    chain_payload["payload"] = None
    assert pub.publish_once() is False

    chain_payload["payload"] = {"symbol": "NIFTY", "transport": "stream"}
    assert pub.publish_once() is True
    assert len(sent) == 1


def test_an_unknown_version_publishes_rather_than_skipping(
        sent, chain_payload, monkeypatch):
    """A duplicate costs a redraw; a wrongly skipped chain freezes the panel."""
    monkeypatch.setattr(ChainPublisher, "_version", staticmethod(lambda: None))
    pub = ChainPublisher()
    assert pub.publish_once() is True
    assert pub.publish_once() is True
    assert len(sent) == 2


def test_a_redis_failure_is_counted_and_not_raised(
        monkeypatch, chain_payload, version):
    monkeypatch.setattr(chain_publisher, "publish",
                        lambda *a, **k: False)
    pub = ChainPublisher()
    assert pub.publish_once() is False
    assert pub.failures == 1
    assert pub.last_error


def test_a_failed_publish_is_retried_rather_than_treated_as_sent(
        monkeypatch, chain_payload, version):
    """A dropped publish must not advance the version, or the chain that
    failed to go out is never sent at all."""
    calls = []
    monkeypatch.setattr(chain_publisher, "publish",
                        lambda *a, **k: (calls.append(1), False)[1])
    pub = ChainPublisher()
    pub.publish_once()
    pub.publish_once()
    assert len(calls) == 2


def test_the_loop_survives_a_failing_cycle(monkeypatch):
    """A publisher that dies takes the chain off every dashboard while the
    feed underneath goes on looking perfectly healthy."""
    pub = ChainPublisher()
    boom = {"n": 0}

    def explode():
        boom["n"] += 1
        if boom["n"] >= 2:
            pub._stopping.set()
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(pub, "publish_once", explode)
    monkeypatch.setattr(pub, "_interval", lambda: 0.0)
    pub._run()

    assert boom["n"] == 2
    assert pub.failures == 2
    assert "RuntimeError" in pub.last_error


def test_it_does_not_start_when_option_streaming_is_off(monkeypatch):
    monkeypatch.setenv("ANGEL_OPTIONS_ENABLED", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        assert ChainPublisher().start() is False
    finally:
        get_settings.cache_clear()


def test_the_interval_tracks_the_price_floor(monkeypatch):
    """Both feeds share one cadence constant, so they cannot drift apart."""
    monkeypatch.setenv("ANGEL_MIN_PUBLISH_MS", "400")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        assert ChainPublisher()._interval() == pytest.approx(0.4)
    finally:
        get_settings.cache_clear()


def test_a_zero_floor_does_not_spin_the_thread(monkeypatch):
    monkeypatch.setenv("ANGEL_MIN_PUBLISH_MS", "0")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        assert ChainPublisher()._interval() == pytest.approx(0.05)
    finally:
        get_settings.cache_clear()


def test_status_reports_whether_it_is_actually_running():
    """A chain streaming into a publisher that is not running is still
    three seconds from the screen."""
    status = ChainPublisher().status()
    assert status["running"] is False
    assert status["channel"] == CHAIN_CHANNEL
    assert "published" in status and "skipped_unchanged" in status


def test_stop_is_safe_on_a_publisher_that_never_started():
    ChainPublisher().stop()
