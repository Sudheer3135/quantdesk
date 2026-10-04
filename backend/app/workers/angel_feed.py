"""The Angel One websocket feed, supervised.

The desk's price used to be a five-second poll. This replaces it with a push
feed and keeps the poll as the fallback, which is the whole design: two
sources, one payload shape, and an explicit answer at every moment to "which
one am I looking at?".

**The supervisor is the point.** SmartWebSocketV2 has its own retry, but a
retry that gives up leaves a silent socket and a dashboard showing the last
price it ever received — the same failure that cost this desk two sessions
of option data, in a different place. So this owns the loop: connect, and if
`connect()` ever returns or raises, back off and do it again, re-logging in
when the session is old enough to be the likely cause. A feed that stops is
a feed that reconnects, and every attempt is counted.

**Liveness is measured, not assumed.** A push feed proves it is alive by
pushing, so the only honest health check is "when did a tick last arrive?".
Past `angel_stale_seconds` the feed reports itself unhealthy, the poller
takes over on its next run, and the transition is counted as a stale event
rather than being smoothed over. A socket that is connected but silent is
exactly the failure a connection check would miss.

**Nothing here decides anything.** It publishes a price. Signals, regime,
bias, entry state and risk are untouched and still come only from the agent.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..brokers import angel as angel_api
from ..brokers.angel import AngelError, AngelNotConfigured, MalformedTick
from ..config import get_settings
from ..market_hours import is_open as market_is_open
from . import option_chain_live, prices, vix_live

log = logging.getLogger(__name__)

# What the feed is doing, as the health endpoint reports it.
IDLE = "idle"                 # not started, or switched off
CONNECTING = "connecting"
LIVE = "live"                 # connected and ticks are arriving
STALE = "stale"               # connected, but nothing has arrived recently
DOWN = "down"                 # not connected; reconnecting

# Source labels, which end up on every published price.
ANGEL = "angel"

# How long a session is trusted before a reconnect re-authenticates rather
# than reusing the tokens. Angel's tokens last the trading day; re-logging
# in on every blip would burn TOTP attempts, and never re-logging in means a
# feed that dies at 09:30 stays dead because it keeps offering a token the
# server has since expired.
SESSION_MAX_AGE_SECONDS = 6 * 3600

# Measured on 21-Sep-2026: the Mac moved from wifi to an iPhone hotspot at
# 09:58 and the feed never came back. `lsof` showed the Angel socket still
# ESTABLISHED from 10.124.17.242 — an address no interface held any more.
# No FIN or RST can arrive on a socket whose source address is gone, so the
# kernel kept it open and the SDK kept believing it was connected.
#
# Both numbers below exist to make that state impossible to stay in.
#
# The socket timeout is the important one. websocket-client only puts a
# timeout on the underlying socket if one is set globally — `_app.py` does
# `self.sock.settimeout(getdefaulttimeout())` and nothing else — so without
# it every send() and recv() on a stranded socket blocks indefinitely. That
# is what wedged the watchdog: it called close_connection(), which sends a
# close frame *before* setting any timeout of its own, and never returned.
# Only reached when the peer is unreachable; the read loop selects on the
# socket rather than sitting in recv(), so a quiet market does not trip it.
SOCKET_TIMEOUT_SECONDS = 15.0

# Ping/pong is how a peer that is reachable but silent gets noticed. The SDK
# passes ping_interval=10 and no ping_timeout, which sends pings and never
# checks that a pong came back. With this set, websocket-client terminates
# run_forever when a pong is late, `connect()` returns, and `_supervise`
# reconnects on its own backoff — the recovery path that was already here
# and could never run. Must stay below the SDK's 10s ping_interval, which
# websocket-client enforces (`Ensure ping_interval > ping_timeout`).
#
# Judged on pongs, not on ticks, so a quiet-but-healthy socket outside
# market hours is not mistaken for a dead one.
PING_TIMEOUT_SECONDS = 5.0


@dataclass
class FeedStats:
    """Everything the feed knows about its own behaviour.

    Counters are cumulative for the process lifetime. `last_tick_at` is what
    health is actually decided on — the rest is for the report.
    """
    state: str = IDLE
    ticks: int = 0
    published: int = 0
    throttled: int = 0
    malformed: int = 0
    connects: int = 0
    reconnects: int = 0
    subscribes: int = 0
    logins: int = 0
    login_failures: int = 0
    socket_errors: int = 0
    stale_events: int = 0
    fallbacks: int = 0
    # Options ride the same socket but are counted apart. Folding them
    # into `ticks` would let a busy chain disguise a silent index.
    option_ticks: int = 0
    option_malformed: int = 0
    option_subscribes: int = 0
    # India VIX shares the index subscription and is counted apart for the
    # same reason options are.
    vix_ticks: int = 0
    # Polls the debounce suppressed: the feed was quiet but not yet
    # quiet enough to justify changing source. A rising count here with
    # stale_events flat is the anti-flap gate doing its job.
    held_through: int = 0
    # How many times the watchdog force-closed a socket that was still
    # reporting itself connected but had gone silent past
    # angel_force_reconnect_seconds. Each one is a stall the vendor SDK's
    # own reconnect had not yet noticed on its own.
    forced_reconnects: int = 0
    last_tick_at: datetime | None = None
    last_source_time: datetime | None = None
    last_price: float | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    # Rolling latency samples, bounded. A feed that has run all session
    # would otherwise accumulate a list of every tick it ever saw.
    feed_latency_ms: list[float] = field(default_factory=list)
    publish_latency_ms: list[float] = field(default_factory=list)
    # Time between consecutive ticks. This is what `angel_stale_seconds`
    # is really a threshold on, and it was never measured — the 10s
    # figure was chosen, not derived. A p99 well under it means the
    # threshold is safe; a p99 near it means the feed was always going
    # to flap and the number needs raising on evidence.
    gap_ms: list[float] = field(default_factory=list)


SAMPLE_LIMIT = 2000


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return round(ordered[index], 2)


class AngelFeed:
    """Owns the Angel session, the socket, and the answer to "is it live?"."""

    def __init__(self, *, login_fn=None, socket_factory=None,
                 publish_fn=None, clock=None) -> None:
        # Seams, all four. The socket blocks and the login needs a real
        # account, so a feed that could only be exercised against production
        # would be a feed nobody tests.
        self._login = login_fn or angel_api.login
        self._socket_factory = socket_factory or _build_socket
        self._publish = publish_fn or prices.publish_price
        self._now = clock or (lambda: datetime.now(UTC))

        self.stats = FeedStats()
        self._session = None
        self._socket = None
        self._thread: threading.Thread | None = None
        self._options_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._last_publish_at: datetime | None = None
        self._was_healthy = False
        self._unhealthy_streak = 0
        # When the watchdog last forced a reconnect, so it kicks a given
        # stall once and then waits rather than calling close_connection()
        # on an already-closing socket every few seconds until a fresh tick
        # finally arrives.
        self._last_forced_reconnect_at: datetime | None = None
        # The universe that last reached the socket *successfully*. Not
        # the same thing as the one that is installed: a subscribe can
        # fail on its own, and the difference between the two is what
        # tells the maintenance loop to try again.
        self._subscribed_universe = None

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> bool:
        """Begin supervising. Returns whether the feed was started.

        Returns False rather than raising when Angel is switched off or
        incompletely configured: a missing credential must not stop the
        application from booting, it must leave the desk on the free feed
        and say so.
        """
        settings = get_settings()
        if not settings.angel_enabled:
            log.info("Angel feed disabled (ANGEL_ENABLED is not set) — "
                     "live prices come from the %s broker.", settings.broker)
            return False

        try:
            angel_api.load_credentials()
        except AngelNotConfigured as exc:
            # A configuration mistake, named. Not an outage.
            log.error("Angel feed not started: %s", exc)
            self.stats.last_error = str(exc)
            return False

        if self._thread and self._thread.is_alive():
            return True

        self._stopping.clear()
        self.stats.started_at = self._now()
        self.stats.state = CONNECTING
        self._thread = threading.Thread(
            target=self._supervise, name="angel-feed", daemon=True)
        self._thread.start()

        if settings.angel_options_enabled:
            self._options_thread = threading.Thread(
                target=self._maintain_options, name="angel-options", daemon=True)
            self._options_thread.start()

        # Unconditional — this protects the core price feed, not an
        # opt-in enhancement riding on top of it.
        self._watchdog_thread = threading.Thread(
            target=self._watch_for_stall, name="angel-watchdog", daemon=True)
        self._watchdog_thread.start()

        log.info("Angel feed starting for token %s", settings.angel_nifty_token)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Close the socket and stop supervising.

        Idempotent, and safe to call on a feed that never started — the
        application's shutdown path must not need to know which.
        """
        self._stopping.set()
        socket = self._socket
        self._socket = None
        if socket is not None:
            # Off-thread for the same reason the watchdog is: a socket
            # stranded by a network change never finishes closing, and
            # shutdown must not wait on one. The join below is already
            # bounded, so a socket that will not answer delays nothing.
            threading.Thread(
                target=self._close_quietly, args=(socket,),
                name="angel-close", daemon=True).start()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.stats.state = IDLE
        log.info("Angel feed stopped after %d tick(s)", self.stats.ticks)

    # ---- health --------------------------------------------------------

    def age_seconds(self) -> float | None:
        """How long since a tick arrived. None when none ever has."""
        if self.stats.last_tick_at is None:
            return None
        return (self._now() - self.stats.last_tick_at).total_seconds()

    @property
    def healthy(self) -> bool:
        """Is Angel the source the desk should be using right now?

        Two conditions, and the second is not redundant.

        *A tick arrived recently.* A socket that is connected but has not
        pushed for ten seconds is not healthy, because the price on the
        screen is ten seconds old either way.

        *That tick was itself current.* On subscribe Angel replays the
        instrument's last traded price, which before the open is the
        previous session's close. Judging on arrival alone would mark the
        feed healthy off one replayed tick, suppress the poller, and leave
        the desk holding a price from yesterday — with `freshness` correctly
        reporting "stale" and nothing acting on it. Health has to be about
        the price, not about the socket.
        """
        if not get_settings().angel_enabled or self._stopping.is_set():
            return False
        age = self.age_seconds()
        if age is None or age > get_settings().angel_stale_seconds:
            return False

        printed = self.stats.last_source_time
        if printed is None:
            return False
        return (self._now() - printed).total_seconds() <= prices.DELAYED_SECONDS

    def should_poll(self) -> bool:
        """Whether the poller should serve a price this cycle.

        The anti-flap gate, and the reason the source no longer changes on a
        single missed beat. `healthy` is a snapshot — true or false right
        now — and acting on it directly meant one late tick past
        `angel_stale_seconds` published a polled quote, flipped `source` to
        the slower feed, and flipped back on the next tick. The dashboard
        showed that as fluctuation; underneath, a 2.2s-old Yahoo quote was
        briefly replacing a 0.4s-old streamed one, which is a worse price by
        the only measure that matters.

        So a switch has to be *earned*: the feed must fail
        `angel_fallback_confirmations` consecutive checks. Below that the
        poller stands down and the last streamed price stands, which is
        still the freshest thing anyone has.

        Recovery is deliberately not debounced. One good tick and the
        streamed feed is serving again — waiting to trust a feed that is
        demonstrably working would be latency invented for its own sake.
        """
        if not get_settings().angel_enabled:
            return True                     # Angel off: the poll is the source

        if self.healthy:
            self._unhealthy_streak = 0
            self._was_healthy = True
            return False

        self._unhealthy_streak += 1
        if self._unhealthy_streak < get_settings().angel_fallback_confirmations:
            self.stats.held_through += 1
            log.debug("Angel quiet for %d check(s) — holding the streamed "
                      "price rather than switching", self._unhealthy_streak)
            return False

        self.note_fallback()
        return True

    def note_fallback(self) -> None:
        """Record that the poller served a price because we could not.

        Called by the ticker. The first fallback after a healthy period is
        also counted as a stale event, so the report distinguishes one long
        outage from a feed that keeps flapping.
        """
        self.stats.fallbacks += 1
        if self._was_healthy:
            self.stats.stale_events += 1
            self._was_healthy = False
            log.warning(
                "Angel feed went quiet (%.1fs since the last tick) — falling "
                "back to the polled source.",
                self.age_seconds() if self.age_seconds() is not None else -1)
        if self.stats.state == LIVE:
            self.stats.state = STALE

    def status(self) -> dict:
        """The feed's own account of itself, with no credentials in it.

        Everything here is a counter, a timestamp or a state name. The
        session is rendered through `redacted`, which is the only shape
        allowed out of the backend.
        """
        settings = get_settings()
        age = self.age_seconds()
        stats = self.stats
        return {
            "enabled": settings.angel_enabled,
            "state": stats.state,
            "healthy": self.healthy,
            "source": ANGEL if self.healthy else settings.broker,
            "transport": "stream" if self.healthy else "poll",
            "token": settings.angel_nifty_token,
            "stale_after_seconds": settings.angel_stale_seconds,
            "seconds_since_tick": round(age, 3) if age is not None else None,
            "last_price": stats.last_price,
            "last_tick_at": stats.last_tick_at.isoformat() if stats.last_tick_at else None,
            "last_source_time": (stats.last_source_time.isoformat()
                                 if stats.last_source_time else None),
            "started_at": stats.started_at.isoformat() if stats.started_at else None,
            "counters": {
                "ticks": stats.ticks,
                "published": stats.published,
                "throttled": stats.throttled,
                "malformed": stats.malformed,
                "connects": stats.connects,
                "reconnects": stats.reconnects,
                "resubscribes": stats.subscribes,
                "logins": stats.logins,
                "login_failures": stats.login_failures,
                "socket_errors": stats.socket_errors,
                "stale_events": stats.stale_events,
                "fallbacks": stats.fallbacks,
                "held_through": stats.held_through,
                "forced_reconnects": stats.forced_reconnects,
                "option_ticks": stats.option_ticks,
                "vix_ticks": stats.vix_ticks,
            },
            "latency_ms": {
                # Read `feed_min` first, not `feed_p50`.
                #
                # Angel rounds `exchange_timestamp` to a whole second, so
                # each feed sample is the true transit plus up to 1000ms of
                # rounding, spread flat across that second. The percentiles
                # therefore mostly measure the rounding: sampled
                # 15-Sep-2026, p50 came to 562ms on a feed whose ticks were
                # 97ms apart — the p50 was the artefact, not the feed.
                #
                # The minimum is the one statistic that rounding cannot
                # inflate. Over a window some tick lands hard against the
                # second boundary with almost no rounding left in it, so
                # the floor converges on the real transit time; that same
                # sample put it at 56ms. It is a floor, not an average, and
                # the truth sits somewhere in [feed_min, feed_min +
                # source_quantum_ms] — which is as narrow as this vendor's
                # clock allows anyone to be.
                "feed_min": (round(min(stats.feed_latency_ms), 2)
                             if stats.feed_latency_ms else None),
                "feed_p50": _percentile(stats.feed_latency_ms, 50),
                "feed_p95": _percentile(stats.feed_latency_ms, 95),
                # The width of the uncertainty above, so nobody has to
                # rediscover it from the shape of a histogram. Read off the
                # last tick rather than hard-coded, so the day Angel starts
                # sending milliseconds this reports 0.0 and the caveat
                # retires itself instead of outliving the problem.
                "source_quantum_ms": prices.source_quantum_ms(
                    stats.last_source_time),
                # Ours, and measured against our own clock at both ends, so
                # these carry no such caveat.
                "publish_p50": _percentile(stats.publish_latency_ms, 50),
                "publish_p95": _percentile(stats.publish_latency_ms, 95),
                "samples": len(stats.feed_latency_ms),
            },
            # What `angel_stale_seconds` is actually a threshold on.
            "tick_gap_ms": {
                "p50": _percentile(stats.gap_ms, 50),
                "p95": _percentile(stats.gap_ms, 95),
                "p99": _percentile(stats.gap_ms, 99),
                "max": round(max(stats.gap_ms), 2) if stats.gap_ms else None,
                "threshold_ms": get_settings().angel_stale_seconds * 1000,
                "samples": len(stats.gap_ms),
            },
            "session": self._session.redacted if self._session else None,
            "last_error": stats.last_error,
        }

    # ---- the supervisor -------------------------------------------------

    def _supervise(self) -> None:
        settings = get_settings()
        backoff = settings.angel_reconnect_min_seconds

        while not self._stopping.is_set():
            try:
                self._ensure_session()
                self._connect_once()
                # `connect()` returning is a disconnect, not a success.
                if not self._stopping.is_set():
                    log.warning("Angel socket closed; reconnecting.")
            except AngelError as exc:
                self.stats.login_failures += 1
                self.stats.last_error = str(exc)
                log.error("Angel feed authentication problem: %s", exc)
                self._session = None
            except Exception as exc:                      # noqa: BLE001
                self.stats.socket_errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Angel socket error: %s", self.stats.last_error)

            if self._stopping.is_set():
                break

            self.stats.state = DOWN
            self.stats.reconnects += 1
            # Wait on the stop event rather than sleeping, so shutdown does
            # not have to outlast a sixty-second backoff.
            self._stopping.wait(backoff)
            backoff = min(backoff * 2, settings.angel_reconnect_max_seconds)
            # A session that is old is the likelier cause of a repeated
            # failure than the network, so drop it and log in again.
            if self._session_is_old():
                self._session = None

        self.stats.state = IDLE

    def _session_is_old(self) -> bool:
        if self._session is None:
            return True
        return ((self._now() - self._session.created_at).total_seconds()
                > SESSION_MAX_AGE_SECONDS)

    def _ensure_session(self) -> None:
        if self._session is not None and not self._session_is_old():
            return
        self._session = self._login()
        self.stats.logins += 1
        log.info("Angel session established for %s",
                 self._session.redacted["client_code"])

    def _connect_once(self) -> None:
        self.stats.state = CONNECTING
        socket = self._socket_factory(self._session)

        socket.on_open = self._on_open
        socket.on_data = self._on_data
        socket.on_error = self._on_error
        socket.on_close = self._on_close

        self._socket = socket
        self.stats.connects += 1
        # Blocks until the socket closes. The SDK's own retry may reconnect
        # underneath this; if it gives up, we return and the supervisor
        # takes over.
        socket.connect()

    # ---- socket callbacks ------------------------------------------------

    def _on_open(self, _wsapp=None) -> None:
        """Subscribe on every open — which is also the resubscribe path.

        The SDK can reconnect underneath us, and a reconnected socket has no
        subscriptions. Subscribing here rather than once after the first
        connect is what stops a reconnect producing a permanently silent
        feed that still reports itself connected.
        """
        settings = get_settings()
        # India VIX rides the index subscription: same segment, same mode,
        # one round trip. `_on_data` routes it by token before the index
        # path, so it cannot be published as the NIFTY price.
        tokens = [settings.angel_nifty_token]
        if settings.angel_vix_token:
            tokens.append(settings.angel_vix_token)
        try:
            self._socket.subscribe(
                "quantdesk-nifty", angel_api.LTP_MODE,
                [{"exchangeType": int(settings.angel_exchange_type),
                  "tokens": [str(t) for t in tokens]}])
            self.stats.subscribes += 1
            self.stats.state = LIVE
            log.info("Angel feed subscribed to %s (LTP), subscription #%d",
                     ", ".join(tokens), self.stats.subscribes)
        except Exception as exc:                          # noqa: BLE001
            self.stats.last_error = f"subscribe failed: {exc}"
            log.error("Angel subscribe failed: %s", exc)

        # After the index, and in its own try: the price is why this socket
        # exists, and a chain that fails to subscribe must not take it down.
        if settings.angel_options_enabled:
            self._subscribe_options()

    def _on_error(self, *args) -> None:
        self.stats.socket_errors += 1
        self.stats.last_error = f"socket error: {args[-1] if args else 'unknown'}"
        log.warning("Angel socket reported an error: %s", self.stats.last_error)

    def _on_close(self, *_args) -> None:
        self.stats.state = DOWN
        log.info("Angel socket closed.")

    def _on_data(self, _wsapp, message=None) -> None:
        """One tick: decode it, throttle it, publish it.

        Never raises. This runs on the SDK's reader thread, and an exception
        escaping here kills the socket — turning one malformed frame into a
        disconnection and, worse, into a reconnect loop if the frame repeats.
        """
        payload = message if message is not None else _wsapp
        received_at = self._now()

        # Options arrive on the same socket in a different mode. Routed
        # before the index decoder rather than after, because an option
        # frame carries a zero LTP whenever the strike has not traded and
        # `decode_tick` — correctly, for an index — rejects that as
        # unusable. Sending option frames down that path would report every
        # quiet strike as a malformed tick.
        if self._is_option_frame(payload):
            self._on_option_data(payload, received_at)
            return

        try:
            tick = angel_api.decode_tick(payload, now=received_at)
        except MalformedTick as exc:
            self.stats.malformed += 1
            self.stats.last_error = f"malformed tick: {exc}"
            log.debug("discarded a malformed Angel tick: %s", exc)
            return
        except Exception as exc:                          # noqa: BLE001
            self.stats.malformed += 1
            self.stats.last_error = f"tick handler: {type(exc).__name__}: {exc}"
            log.warning("Angel tick handler failed: %s", exc)
            return

        # Before anything touches the index's state. A VIX print counted as
        # an index tick would hold `healthy` up on a dead NIFTY feed, and one
        # published would draw the index at fifteen.
        vix_token = get_settings().angel_vix_token
        if vix_token and tick.token == str(vix_token):
            self.stats.vix_ticks += 1
            vix_live.VIX.update(tick)
            return

        with self._lock:
            if self.stats.last_tick_at is not None:
                _record(self.stats.gap_ms,
                        (received_at - self.stats.last_tick_at).total_seconds() * 1000)
            self.stats.ticks += 1
            self.stats.last_tick_at = received_at
            self.stats.last_source_time = tick.source_time
            self.stats.last_price = tick.price
            self.stats.state = LIVE
            self._was_healthy = True

            if self._throttled(received_at):
                self.stats.throttled += 1
                return
            self._last_publish_at = received_at

        try:
            published = self._publish(
                get_settings().watch_symbol, tick.price,
                source=ANGEL,
                source_time=tick.source_time.isoformat(),
                received_at=received_at,
                transport="stream")
        except Exception as exc:                          # noqa: BLE001
            # Publishing already swallows Redis failures; anything reaching
            # here is a bug, and it still must not kill the reader thread.
            self.stats.last_error = f"publish: {type(exc).__name__}: {exc}"
            log.warning("Angel tick publish failed: %s", exc)
            return

        self.stats.published += 1
        _record(self.stats.feed_latency_ms, published.get("feed_latency_ms"))
        _record(self.stats.publish_latency_ms, published.get("publish_latency_ms"))

    # ---- options -------------------------------------------------------

    def _maintain_options(self, master_fn=None, interval: float = 30.0) -> None:
        """Choose the contracts to watch, and re-choose when spot drifts.

        Its own thread for two reasons. Building a universe means fetching
        the instrument master — some 140k rows and about ten seconds — and
        doing that on the reader thread would drop ticks; doing it in
        `_supervise` would delay the reconnect that loop exists to perform.

        Waits for a price before choosing anything. The band is centred on
        spot, so a universe built before the first tick would be centred on
        a guess.
        """
        from ..data import option_universe

        master = None
        while not self._stopping.is_set():
            settings = get_settings()
            spot = self.stats.last_price
            if not settings.angel_options_enabled or spot is None:
                self._stopping.wait(interval)
                continue

            current = option_chain_live.CHAIN.universe
            if current and not current.needs_refresh(
                    spot, margin=settings.angel_options_refresh_margin):
                # Installed is not the same as subscribed. `_subscribe_
                # options` swallows its own failures on purpose — the
                # index feed is why this socket exists and the chain must
                # not be able to cost the price — but nothing then ever
                # tried again, so a single failed subscribe left the
                # universe sitting in memory, `needs_refresh` answering
                # "no" quite correctly, and the loop skipping past it for
                # the rest of the session.
                #
                # Measured 16-Sep-2026: "option subscribe failed:
                # Connection is already closed" at the open, and the live
                # chain served 0 of 80 contracts all morning while the
                # desk quietly ran on the 60-second NSE poll.
                if self._subscribed_universe is not current \
                        and self._socket is not None:
                    self._subscribe_options()
                if master is not None and settings.v2_paper_enabled:
                    self._maintain_v2_universe(master, spot, settings)
                self._stopping.wait(interval)
                continue

            try:
                if master is None:
                    loader = master_fn or _load_master
                    master = loader()
                universe = option_universe.build(
                    master, spot, underlying=settings.watch_symbol,
                    band=settings.angel_options_band)
            except Exception as exc:                      # noqa: BLE001
                # The chain is an enhancement; the price is the product.
                # A master that will not load leaves the desk on the polled
                # chain and says so, rather than taking the feed with it.
                self.stats.last_error = f"option universe: {type(exc).__name__}: {exc}"
                log.warning("could not build the option universe: %s", exc)
                self._stopping.wait(interval)
                continue

            if universe.contracts:
                option_chain_live.CHAIN.max_age_seconds = \
                    settings.angel_options_max_age_seconds
                option_chain_live.CHAIN.set_universe(universe)
                if self._socket is not None:
                    self._subscribe_options()
                log.info("option universe re-centred on %.2f: %s",
                         spot, universe.to_dict())
            else:
                # Says so, rather than going round the loop in silence.
                # An empty universe is not a quiet state: it means the live
                # option chain never subscribes, so every option on the
                # desk quietly drops to the polled NSE snapshot a minute
                # behind the market. On 15-Sep-2026 that ran for seven
                # minutes and the only way to find it was to notice the
                # subscription count sitting at zero.
                self.stats.last_error = (
                    f"option universe empty at spot {spot:.2f}")
                log.warning(
                    "option universe came back empty at spot %.2f — the "
                    "live chain cannot subscribe and options are being "
                    "served from the polled snapshot instead", spot)
            if settings.v2_paper_enabled:
                self._maintain_v2_universe(master, spot, settings)
            self._stopping.wait(interval)

    def _maybe_force_reconnect(self) -> bool:
        """One check: is the socket stalled, and if so, kick it.

        `angel_stale_seconds` only decides when the poller starts covering
        for the feed — it never touches the socket. Measured on
        15-Sep-2026: twice, ticks stopped for minutes (677s and 365s) while
        the socket still reported itself connected. Neither `on_close` nor
        `on_error` fired, so `_supervise`'s reconnect loop — which only
        acts on those — never ran; what eventually recovered it both times
        was the vendor SDK's own internal reconnect, logged by its own
        `websocket` logger rather than ours, on a clock measured that day
        at minutes rather than seconds.

        This closes the gap without touching that internal logic: past
        `angel_force_reconnect_seconds` of silence, `close_connection()` is
        called here, exactly as `stop()` already does on shutdown. The
        supervisor sees that as an ordinary disconnect — `on_close` fires,
        `connect()` returns — and reconnects at its own 2-60s backoff
        instead of whatever the vendor's internal clock decides.

        Gated on market hours: a quiet socket after the close is the
        market being shut, not a stall, and force-reconnecting all night
        would just be a login loop against a server with nothing to send.

        Returns whether it kicked, which is all a test needs to assert on;
        the loop below only cares that this ran.
        """
        if not market_is_open():
            return False

        settings = get_settings()
        age = self.age_seconds()
        threshold = settings.angel_force_reconnect_seconds
        socket = self._socket
        if age is None or age <= threshold or socket is None:
            return False

        since_last_kick = (
            (self._now() - self._last_forced_reconnect_at).total_seconds()
            if self._last_forced_reconnect_at else None)
        # One kick per stall. Without this the loop would call
        # close_connection() on an already-closing socket every `interval`
        # seconds until a fresh tick finally arrives — harmless to the
        # vendor call, but noisy and pointless. The cooldown re-arms on
        # `angel_reconnect_max_seconds`, the same ceiling the supervisor's
        # own backoff caps at, so a stall that survives one kick still gets
        # another rather than being kicked only once ever.
        cooldown = settings.angel_reconnect_max_seconds
        if since_last_kick is not None and since_last_kick <= cooldown:
            return False

        self.stats.forced_reconnects += 1
        self._last_forced_reconnect_at = self._now()
        log.warning(
            "Angel socket silent for %.0fs (past the %.0fs ceiling) while "
            "still reporting connected — forcing it closed so the "
            "supervisor reconnects rather than waiting on the SDK's own "
            "recovery.", age, threshold)
        # Off-thread, and this is the whole point of the indirection.
        # close_connection() sends a close frame, and websocket-client sets
        # the socket's timeout only *after* that send. On a socket whose
        # local address is gone the send cannot complete, so calling this
        # inline wedged the watchdog itself: on 21-Sep-2026 it kicked once
        # at 09:58:34 and never ran again, leaving the feed dead for the
        # rest of the session with `forced_reconnects` frozen at 1.
        # A thrown-away daemon thread means a socket that refuses to close
        # costs one parked thread instead of the recovery mechanism.
        threading.Thread(
            target=self._close_quietly, args=(socket,),
            name="angel-force-close", daemon=True).start()
        return True

    @staticmethod
    def _close_quietly(socket) -> None:
        """Close a socket that may never answer. Runs on its own thread."""
        try:
            socket.close_connection()
        except Exception as exc:                             # noqa: BLE001
            log.warning("forced Angel socket close failed: %s", exc)

    def _watch_for_stall(self, interval: float = 5.0) -> None:
        """The loop: check for a stall every `interval`, until stopped.

        The body is guarded because this thread is the only thing that
        notices a stalled socket. An exception escaping here would kill it
        silently and leave the feed with no way back for the whole session
        — which is the failure this watchdog exists to prevent.
        """
        while not self._stopping.is_set():
            try:
                self._maybe_force_reconnect()
            except Exception as exc:                         # noqa: BLE001
                log.warning("Angel stall watchdog check failed: %s", exc)
            self._stopping.wait(interval)

    @staticmethod
    def _is_option_frame(payload) -> bool:
        """Does this frame belong to the option chain rather than the index?

        Decided on the exchange segment, not the mode. The index is NSE_CM
        and the options are NSE_FO, and that stays true whatever mode either
        is subscribed in — whereas keying on SNAP_QUOTE would misroute the
        moment the index subscription is ever widened.
        """
        if not isinstance(payload, dict):
            return False
        return payload.get("exchange_type") == angel_api.NSE_FO

    def _on_option_data(self, payload, received_at: datetime) -> None:
        """One SNAP_QUOTE frame into the live chain. Never raises.

        Option ticks are not published to the price channel and never touch
        `last_price` or the health state. The index feed's liveness is what
        `healthy()` means, and letting a busy option chain hold that flag up
        would mask a dead index feed behind a lively one.
        """
        try:
            tick = angel_api.decode_option_tick(payload, now=received_at)
        except MalformedTick as exc:
            self.stats.option_malformed += 1
            log.debug("discarded a malformed Angel option tick: %s", exc)
            return
        except Exception as exc:                          # noqa: BLE001
            self.stats.option_malformed += 1
            self.stats.last_error = f"option tick: {type(exc).__name__}: {exc}"
            log.warning("Angel option tick handler failed: %s", exc)
            return

        # v2's contracts live in their own store when they are on a later
        # expiry. Asked first by membership, so the main chain's unknown-
        # token counter keeps meaning "a token nobody subscribed".
        store = (option_chain_live.V2_CHAIN
                 if option_chain_live.V2_CHAIN.knows(tick.token)
                 and not option_chain_live.CHAIN.knows(tick.token)
                 else option_chain_live.CHAIN)
        if store.update(tick):
            self.stats.option_ticks += 1

    def _subscribe_options(self) -> None:
        """Subscribe the option universe, if one has been built.

        Called from `_on_open`, so it is also the resubscribe path. A
        failure here is logged and swallowed: the index feed is the reason
        this socket exists, and losing the chain must not cost the price.
        """
        self._subscribe_v2_options()
        universe = option_chain_live.CHAIN.universe
        if not universe or not universe.tokens:
            return
        try:
            self._socket.subscribe(
                "quantdesk-options", angel_api.SNAP_QUOTE,
                angel_api.token_lists({angel_api.NSE_FO: universe.tokens}))
            self.stats.option_subscribes += 1
            self._subscribed_universe = universe
            log.info("Angel feed subscribed to %d option contracts "
                     "(SNAP_QUOTE), expiry %s",
                     len(universe.tokens), universe.expiry)
        except Exception as exc:                          # noqa: BLE001
            self.stats.last_error = f"option subscribe failed: {exc}"
            log.error("Angel option subscribe failed: %s", exc)

    def _subscribe_v2_options(self) -> None:
        """Subscribe v2's later-expiry contracts, when it has any."""
        universe = option_chain_live.V2_CHAIN.universe
        main = option_chain_live.CHAIN.universe
        if not universe or not universe.tokens:
            return
        if main is not None and main.expiry == universe.expiry:
            return
        try:
            self._socket.subscribe(
                "quantdesk-options-v2", angel_api.SNAP_QUOTE,
                angel_api.token_lists({angel_api.NSE_FO: universe.tokens}))
            self.stats.option_subscribes += 1
            log.info("Angel feed subscribed to %d v2 option contracts, expiry %s",
                     len(universe.tokens), universe.expiry)
        except Exception as exc:                          # noqa: BLE001
            self.stats.last_error = f"v2 option subscribe failed: {exc}"
            log.error("Angel v2 option subscribe failed: %s", exc)

    def _maintain_v2_universe(self, master, spot: float, settings) -> None:
        """Keep v2's expiry streamed when it is not the nearest one.

        Never raises into the options thread: v2 is paper, and the main
        chain it shares a thread with is what the signal engine reads.
        """
        from ..data import option_universe
        from ..market_hours import trading_date
        from ..strategy_v2 import rules
        from ..strategy_v2.config import DEFAULT

        try:
            today = trading_date()
            listed = option_universe.expiries(
                option_universe.index_options(master, settings.watch_symbol), on=today)
            option_chain_live.LISTED.set(listed)
            wanted = rules.choose_expiry(listed, today, DEFAULT)
            main = option_chain_live.CHAIN.universe
            store = option_chain_live.V2_CHAIN
            if wanted is None or (main is not None and main.expiry == wanted):
                if store.universe is not None and store.universe.contracts:
                    store.set_universe(option_universe.Universe(
                        contracts=[], expiry=wanted, centre=spot,
                        band=settings.v2_options_band))
                return
            current = store.universe
            if (current is not None and current.expiry == wanted
                    and not current.needs_refresh(spot, margin=3)):
                return
            universe = option_universe.build(
                master, spot, underlying=settings.watch_symbol,
                band=settings.v2_options_band, expiry=wanted)
            if universe.contracts:
                store.max_age_seconds = settings.angel_options_max_age_seconds
                store.set_universe(universe)
                if self._socket is not None:
                    self._subscribe_v2_options()
        except Exception as exc:                          # noqa: BLE001
            log.warning("could not maintain the v2 option universe: %s", exc)

    def _throttled(self, moment: datetime) -> bool:
        """Is this tick inside the minimum gap between publishes?

        The feed is push, so this is not a poll interval — it caps how often
        a burst can reach Redis and every open browser. The tick is still
        counted and still refreshes liveness; only the fan-out is skipped.
        """
        floor_ms = get_settings().angel_min_publish_ms
        if floor_ms <= 0 or self._last_publish_at is None:
            return False
        gap = (moment - self._last_publish_at).total_seconds() * 1000
        return gap < floor_ms


def _record(samples: list[float], value) -> None:
    if value is None:
        return
    samples.append(float(value))
    if len(samples) > SAMPLE_LIMIT:
        del samples[: len(samples) - SAMPLE_LIMIT]


def _load_master():
    """The instrument master, fetched once per process.

    Indirected through a function so the option-universe thread can be
    tested without a ten-second download.
    """
    from ..data.angel_history import load_master
    return load_master()


def _build_socket(session):
    """The vendor socket, imported at the point of use.

    Subclassed rather than used as-is, for one reason: the SDK's `connect()`
    calls `run_forever(ping_interval=10)` with no `ping_timeout`, so it pings
    a dead peer forever and never concludes anything. Overriding `connect()`
    is the only seam — `wsapp` is constructed inside it, so there is nothing
    to configure from outside.
    """
    try:
        import ssl

        import websocket
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    except ImportError as exc:            # pragma: no cover - packaging
        raise AngelError(
            "smartapi-python is not installed — there is no Angel websocket "
            "to connect to") from exc

    # Global, but scoped in practice: SmartApi is the only websocket-client
    # user in the process. The dashboard's own sockets are Starlette's.
    websocket.setdefaulttimeout(SOCKET_TIMEOUT_SECONDS)

    class _BoundedSocket(SmartWebSocketV2):
        """The vendor socket with a bound on how long it can say nothing."""

        def connect(self):
            headers = {
                "Authorization": self.auth_token,
                "x-api-key": self.api_key,
                "x-client-code": self.client_code,
                "x-feed-token": self.feed_token,
            }
            self.wsapp = websocket.WebSocketApp(
                self.ROOT_URI, header=headers,
                on_open=self._on_open, on_error=self._on_error,
                on_close=self._on_close, on_data=self._on_data,
                on_ping=self._on_ping, on_pong=self._on_pong)
            # The vendor call, plus the one argument it omits.
            self.wsapp.run_forever(
                sslopt={"cert_reqs": ssl.CERT_NONE},
                ping_interval=self.HEART_BEAT_INTERVAL,
                ping_timeout=PING_TIMEOUT_SECONDS)

    return _BoundedSocket(
        session.auth_token, session.api_key,
        session.client_code, session.feed_token,
        max_retry_attempt=2)


# One feed per process, like the scheduler. The ticker asks it whether to
# fall back, and the health endpoint asks it how it is doing.
FEED = AngelFeed()


def start() -> bool:
    return FEED.start()


def stop() -> None:
    FEED.stop()


def status() -> dict:
    return FEED.status()


def healthy() -> bool:
    """Whether Angel is currently serving the desk's price.

    Outside market hours a quiet feed is not a fault — the exchange is shut
    and there is nothing to push — but it is still not *serving* a live
    price, so this stays honest and reports False. The ticker already
    declines to poll a closed market, so nothing polls in its place.
    """
    return FEED.healthy


def should_poll() -> bool:
    """Whether the polled source should serve this cycle."""
    return FEED.should_poll()


def note_fallback() -> None:
    FEED.note_fallback()


def market_is_trading() -> bool:
    """Re-exported so the ticker's fallback logic reads in one place."""
    return market_is_open()
