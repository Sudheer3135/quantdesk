"""The Angel feed's lifecycle: ticks, reconnects, staleness and shutdown.

No socket and no account. `AngelFeed` takes four seams — login, socket
factory, publish, clock — because the parts that decide correctness here are
all timing and state, and a feed that could only be exercised against a live
market at 09:20 on a weekday is a feed nobody tests.

The fake socket below is the important fixture. It behaves the way
SmartWebSocketV2 does in the two ways that matter: `connect()` blocks until
the connection ends, and a reconnected socket arrives with no subscriptions.
"""
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers.angel import AngelError, AngelSession
from app.config import get_settings
from app.workers import prices
from app.workers.angel_feed import LIVE, AngelFeed

MOMENT = datetime(2026, 8, 26, 4, 45, tzinfo=UTC)     # 10:15 IST


@pytest.fixture(autouse=True)
def angel_on(monkeypatch):
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    monkeypatch.setenv("ANGEL_API_KEY", "key")
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "S12345")
    monkeypatch.setenv("ANGEL_MPIN", "1234")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("ANGEL_MIN_PUBLISH_MS", "0")
    get_settings.cache_clear()
    prices.reset_previous()
    yield
    get_settings.cache_clear()


def a_session():
    return AngelSession(auth_token="jwt", feed_token="feed",
                        refresh_token="refresh", client_code="S12345",
                        api_key="key", created_at=MOMENT)


class FakeSocket:
    """Behaves like SmartWebSocketV2 in the two ways that matter.

    `connect()` blocks until something ends it, and a fresh instance carries
    no subscriptions — which is what makes the resubscribe test meaningful.
    """

    instances: list["FakeSocket"] = []

    def __init__(self, session=None, fail_connect=None):
        self.session = session
        self.subscriptions = []
        self.closed = False
        self.fail_connect = fail_connect
        self._released = threading.Event()
        self.on_open = self.on_data = self.on_error = self.on_close = None
        FakeSocket.instances.append(self)

    def connect(self):
        if self.fail_connect:
            raise self.fail_connect
        if self.on_open:
            self.on_open(self)
        self._released.wait(timeout=5)     # blocks, like the real one

    def subscribe(self, correlation_id, mode, token_list):
        self.subscriptions.append((correlation_id, mode, token_list))

    def close_connection(self):
        self.closed = True
        self._released.set()
        if self.on_close:
            self.on_close(self)

    def drop(self):
        """Simulate the far end going away without us asking."""
        self._released.set()


class Clock:
    def __init__(self, start=MOMENT):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def ltp(price_paise=2433455, stamp=None):
    moment = stamp or MOMENT
    return {"token": "99926000", "sequence_number": 1,
            "exchange_timestamp": int(moment.timestamp() * 1000),
            "last_traded_price": price_paise, "subscription_mode": 1}


def a_feed(clock=None, published=None, login_fn=None, socket=None):
    published = published if published is not None else []
    clock = clock or Clock()

    def publish(symbol, price, **kw):
        # The real publisher, with the test's clock injected. Using a stand-in
        # would leave the previous-price memory and the Redis hand-off — the
        # two things a failover actually depends on — untested.
        payload = prices.publish_price(symbol, price, published_at=clock(), **kw)
        published.append(payload)
        return payload

    feed = AngelFeed(
        login_fn=login_fn or a_session,
        socket_factory=socket or (lambda s: FakeSocket(s)),
        publish_fn=publish,
        clock=clock)
    return feed, published, clock


@pytest.fixture(autouse=True)
def clean_sockets():
    FakeSocket.instances = []
    yield
    for socket in FakeSocket.instances:
        socket.drop()


# ---- ticks ---------------------------------------------------------------

def test_a_tick_is_decoded_scaled_and_published():
    feed, published, _ = a_feed()
    feed._on_data(None, ltp(2433455))

    assert len(published) == 1
    assert published[0]["price"] == 24_334.55
    assert published[0]["source"] == "angel"
    assert published[0]["transport"] == "stream"
    assert feed.stats.ticks == 1
    assert feed.stats.published == 1


def test_the_source_timestamp_is_preserved_not_replaced_with_now():
    """The exchange's own clock is the only basis on which staleness can be
    judged. Stamping the tick with our arrival time would make every price
    look perfectly fresh, including the ones that are not."""
    clock = Clock()
    printed = MOMENT - timedelta(seconds=3)
    feed, published, _ = a_feed(clock=clock)
    feed._on_data(None, ltp(stamp=printed))

    assert published[0]["source_time"] == printed.isoformat()
    assert published[0]["received_at"] == MOMENT.isoformat()
    assert published[0]["age_seconds"] == pytest.approx(3.0)


def test_the_two_latencies_are_measured_separately():
    """Feed latency is the exchange and the network — nothing here can
    improve it. Publish latency is ours. A single number would hide which
    of the two is the problem."""
    clock = Clock()
    printed = MOMENT - timedelta(milliseconds=120)
    feed, published, _ = a_feed(clock=clock)

    def slow_publish(symbol, price, **kw):
        clock.advance(0.030)               # 30ms of our own work
        payload = prices.publish_price(symbol, price, published_at=clock(), **kw)
        published.append(payload)
        return payload

    feed._publish = slow_publish
    feed._on_data(None, ltp(stamp=printed))

    assert published[0]["feed_latency_ms"] == pytest.approx(120, abs=1)
    assert published[0]["publish_latency_ms"] == pytest.approx(30, abs=1)


def test_latency_percentiles_are_reported():
    feed, _, clock = a_feed()
    for n in range(10):
        clock.advance(1)
        feed._on_data(None, ltp(stamp=clock() - timedelta(milliseconds=50 + n)))

    latency = feed.status()["latency_ms"]
    assert latency["samples"] == 10
    assert 40 <= latency["feed_p50"] <= 70
    assert latency["feed_p95"] >= latency["feed_p50"]


def test_change_and_direction_survive_across_ticks():
    feed, published, clock = a_feed()
    feed._on_data(None, ltp(2433455))
    clock.advance(1)
    feed._on_data(None, ltp(2433955))

    assert published[1]["previous"] == 24_334.55
    assert published[1]["change"] == pytest.approx(5.0)
    assert published[1]["direction"] == "up"


# ---- malformed ticks -----------------------------------------------------

def test_a_malformed_tick_is_counted_and_the_socket_survives():
    """This runs on the SDK's reader thread. An exception escaping here
    kills the socket, turning one bad frame into a disconnection — and into
    a reconnect loop if the frame repeats."""
    feed, published, _ = a_feed()
    for bad in ({}, "not a dict", None, {"last_traded_price": 0,
                                         "exchange_timestamp": 1}):
        feed._on_data(None, bad)

    assert published == []
    assert feed.stats.malformed == 4
    assert feed.stats.ticks == 0
    assert feed.stats.last_error.startswith("malformed tick")


def test_a_malformed_tick_does_not_refresh_liveness():
    """Otherwise a feed emitting nothing but garbage would report itself
    healthy and keep the poller switched off."""
    feed, _, _ = a_feed()
    feed._on_data(None, {"nonsense": True})

    assert feed.age_seconds() is None
    assert feed.healthy is False


def test_a_publish_failure_does_not_kill_the_reader_thread():
    feed, _, _ = a_feed()
    feed._publish = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("redis gone"))

    feed._on_data(None, ltp())          # must not raise

    assert feed.stats.ticks == 1
    assert feed.stats.published == 0
    assert "redis gone" in feed.stats.last_error


# ---- throttling -----------------------------------------------------------

def test_a_burst_is_throttled_without_losing_liveness(monkeypatch):
    """The floor caps fan-out, not the feed. A throttled tick still proves
    the socket is alive, or a busy market would look like a dead one."""
    monkeypatch.setenv("ANGEL_MIN_PUBLISH_MS", "250")
    get_settings.cache_clear()

    feed, published, clock = a_feed()
    feed._on_data(None, ltp(2433455))
    clock.advance(0.050)
    feed._on_data(None, ltp(2433460))
    clock.advance(0.050)
    feed._on_data(None, ltp(2433465))

    assert len(published) == 1
    assert feed.stats.ticks == 3
    assert feed.stats.throttled == 2
    assert feed.healthy is True


def test_the_throttle_releases_after_the_floor(monkeypatch):
    monkeypatch.setenv("ANGEL_MIN_PUBLISH_MS", "250")
    get_settings.cache_clear()

    feed, published, clock = a_feed()
    feed._on_data(None, ltp(2433455))
    clock.advance(0.300)
    feed._on_data(None, ltp(2433465))

    assert len(published) == 2


# ---- health and staleness -------------------------------------------------

def test_a_feed_that_has_never_ticked_is_not_healthy():
    feed, _, _ = a_feed()
    assert feed.healthy is False
    assert feed.age_seconds() is None


def test_a_feed_goes_unhealthy_when_the_ticks_stop():
    feed, _, clock = a_feed()
    feed._on_data(None, ltp())
    assert feed.healthy is True

    clock.advance(11)                   # past ANGEL_STALE_SECONDS
    assert feed.healthy is False
    assert feed.status()["seconds_since_tick"] == pytest.approx(11)


def test_a_replayed_previous_close_does_not_count_as_a_healthy_feed():
    """On subscribe Angel replays the last traded price. Judging health on
    arrival alone would mark the feed healthy off that one tick, suppress
    the poller, and leave the desk holding yesterday's price."""
    feed, published, clock = a_feed()
    feed._on_data(None, ltp(stamp=MOMENT - timedelta(hours=17)))

    assert feed.stats.ticks == 1
    assert published[0]["freshness"] == "stale"
    assert feed.healthy is False


def test_a_fallback_after_a_healthy_period_is_counted_as_a_stale_event():
    """One long outage and a feed that keeps flapping are different faults
    with different fixes, and a single fallback counter cannot tell them
    apart."""
    feed, _, clock = a_feed()
    feed._on_data(None, ltp())
    clock.advance(11)

    feed.note_fallback()
    feed.note_fallback()
    feed.note_fallback()

    assert feed.stats.fallbacks == 3
    assert feed.stats.stale_events == 1        # one transition, not three

    clock.advance(1)
    feed._on_data(None, ltp(stamp=clock()))    # recovers
    clock.advance(11)
    feed.note_fallback()
    assert feed.stats.stale_events == 2


# ---- connect, subscribe, reconnect ----------------------------------------

def test_connecting_subscribes_to_the_configured_token():
    feed, _, _ = a_feed()
    feed._session = a_session()
    socket = FakeSocket(feed._session)
    feed._socket = socket
    feed._on_open(socket)

    correlation, mode, tokens = socket.subscriptions[0]
    assert mode == 1                                   # LTP
    # India VIX rides the same subscription, routed apart by token.
    assert tokens == [{"exchangeType": 1, "tokens": ["99926000", "99926017"]}]
    assert feed.stats.subscribes == 1
    assert feed.stats.state == LIVE


def test_every_reconnect_resubscribes():
    """A reconnected socket has no subscriptions. Subscribing once after the
    first connect is what makes a reconnect produce a permanently silent
    feed that still reports itself connected."""
    feed, _, _ = a_feed()
    feed._session = a_session()

    for _ in range(3):
        socket = FakeSocket(feed._session)
        feed._socket = socket
        feed._on_open(socket)
        assert len(socket.subscriptions) == 1

    assert feed.stats.subscribes == 3


def test_the_supervisor_reconnects_after_the_socket_drops(monkeypatch):
    monkeypatch.setenv("ANGEL_RECONNECT_MIN_SECONDS", "0.01")
    monkeypatch.setenv("ANGEL_RECONNECT_MAX_SECONDS", "0.02")
    get_settings.cache_clear()

    feed, _, _ = a_feed()
    assert feed.start() is True
    _wait_until(lambda: len(FakeSocket.instances) >= 1)

    FakeSocket.instances[0].drop()                     # far end goes away
    _wait_until(lambda: len(FakeSocket.instances) >= 2)

    assert feed.stats.connects >= 2
    assert feed.stats.reconnects >= 1
    assert len(FakeSocket.instances[1].subscriptions) == 1
    feed.stop()


def test_a_login_failure_is_counted_and_retried(monkeypatch):
    monkeypatch.setenv("ANGEL_RECONNECT_MIN_SECONDS", "0.01")
    monkeypatch.setenv("ANGEL_RECONNECT_MAX_SECONDS", "0.02")
    get_settings.cache_clear()

    attempts = []

    def failing_login():
        attempts.append(1)
        if len(attempts) < 3:
            raise AngelError("Angel refused the login: Invalid totp")
        return a_session()

    feed, _, _ = a_feed(login_fn=failing_login)
    feed.start()
    _wait_until(lambda: len(attempts) >= 3, timeout=3)

    assert feed.stats.login_failures >= 2
    assert "Invalid totp" in feed.stats.last_error
    feed.stop()


def test_a_socket_error_does_not_end_the_supervisor(monkeypatch):
    monkeypatch.setenv("ANGEL_RECONNECT_MIN_SECONDS", "0.01")
    monkeypatch.setenv("ANGEL_RECONNECT_MAX_SECONDS", "0.02")
    get_settings.cache_clear()

    made = []

    def factory(session):
        made.append(1)
        return FakeSocket(session,
                          fail_connect=OSError("connection reset") if len(made) < 3
                          else None)

    feed, _, _ = a_feed(socket=factory)
    feed.start()
    _wait_until(lambda: len(made) >= 3, timeout=3)

    assert feed.stats.socket_errors >= 2
    feed.stop()


# ---- start and stop --------------------------------------------------------

def test_a_disabled_feed_does_not_start(monkeypatch):
    monkeypatch.setenv("ANGEL_ENABLED", "false")
    get_settings.cache_clear()

    feed, _, _ = a_feed()
    assert feed.start() is False
    assert feed.healthy is False


def test_missing_credentials_leave_the_desk_on_the_poller(monkeypatch):
    """A configuration mistake must not stop the application from booting.
    It leaves the price on the polled source and says so."""
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "")
    get_settings.cache_clear()

    feed, _, _ = a_feed()
    assert feed.start() is False
    assert "ANGEL_TOTP_SECRET" in feed.stats.last_error


def test_stopping_closes_the_socket_and_ends_the_thread():
    feed, _, _ = a_feed()
    feed.start()
    _wait_until(lambda: len(FakeSocket.instances) >= 1)

    feed.stop(timeout=3)

    assert FakeSocket.instances[0].closed is True
    assert feed._thread is not None and not feed._thread.is_alive()
    assert feed.status()["state"] == "idle"


def test_stopping_a_feed_that_never_started_is_safe():
    """The application's shutdown path must not need to know whether the
    feed came up."""
    feed, _, _ = a_feed()
    feed.stop()                        # must not raise
    assert feed.status()["state"] == "idle"


def test_stopping_stops_reconnecting(monkeypatch):
    monkeypatch.setenv("ANGEL_RECONNECT_MIN_SECONDS", "0.01")
    get_settings.cache_clear()

    feed, _, _ = a_feed()
    feed.start()
    _wait_until(lambda: len(FakeSocket.instances) >= 1)
    feed.stop(timeout=3)

    before = len(FakeSocket.instances)
    threading.Event().wait(0.2)
    assert len(FakeSocket.instances) == before
    assert feed.stats.state == "idle"


# ---- status ----------------------------------------------------------------

def test_the_status_names_the_serving_source():
    feed, _, clock = a_feed()
    feed._on_data(None, ltp())

    status = feed.status()
    assert status["source"] == "angel"
    assert status["transport"] == "stream"
    assert status["healthy"] is True
    assert status["last_price"] == 24_334.55

    clock.advance(11)
    degraded = feed.status()
    assert degraded["source"] != "angel"
    assert degraded["transport"] == "poll"


def test_the_status_carries_no_credentials():
    feed, _, _ = a_feed()
    feed._session = a_session()
    rendered = str(feed.status())

    for secret in ("jwt", "feed", "refresh", "1234", "JBSWY3DPEHPK3PXP"):
        assert secret not in rendered.replace("feed_p50", "").replace(
            "feed_p95", "").replace("has_feed_token", "").replace(
            "stale_after_seconds", "").replace("the feed", "")


def _wait_until(predicate, timeout=3.0):
    deadline = threading.Event()
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        deadline.wait(0.02)
        waited += 0.02
    raise AssertionError("condition never became true")
