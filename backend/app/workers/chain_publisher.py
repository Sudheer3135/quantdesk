"""Push the streamed option chain to browsers instead of making them ask.

The index price has been a push all along: Angel prints a tick, the feed
publishes it to Redis, and `/ws/signals` fans it out to every open
dashboard. The option chain was not. It arrived over the same websocket,
landed in `CHAIN` within a fraction of a millisecond — and then sat there
until a browser happened to poll `/market/option-chain`, which it did every
three seconds.

So the desk had two live feeds arriving together and reaching the screen
three seconds apart. That gap is not merely slow, it is incoherent: the
dashboard prices an option decision against a spot price from a different
moment, and nothing on screen says so.

This closes it. One thread, one snapshot per publish, fanned out on the
socket the price already uses.

Two things it deliberately does *not* do:

*Publish from the tick handler.* That handler runs on the websocket reader
thread — the same thread carrying the index price. Building a chain payload
there (a pandas summarise over forty strikes) would put the option chain's
CPU cost directly in the price's latency path, making the faster feed slower
to speed up the slower one. This thread is separate, so a slow publish can
never stall the socket.

*Publish every tick.* Eighty contracts printing independently would fan out
eighty near-identical chains a second. The floor is `angel_min_publish_ms` —
the same constant that bounds the price — so both feeds reach the browser on
the same cadence by construction rather than by coincidence.
"""
from __future__ import annotations

import json
import logging
import threading

from ..cache import publish
from ..config import get_settings

log = logging.getLogger(__name__)

CHAIN_CHANNEL = "chain"
CACHE_KEY = "chain:stream:latest"

# Long enough that a browser reconnecting mid-session is handed a chain
# rather than a blank panel, short enough that it cannot serve one from a
# previous session as though it were current.
CACHE_TTL_SECONDS = 120


class ChainPublisher:
    """Fans the streamed chain out to Redis on a bounded cadence."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.published = 0
        self.skipped_unchanged = 0
        self.failures = 0
        self.last_error: str | None = None
        self._last_version: int | None = None

    def start(self) -> bool:
        """Begin publishing. Returns whether the thread was started.

        Returns False rather than raising when option streaming is off, for
        the same reason the feed does: a desk configured for the polled
        chain must still boot.
        """
        settings = get_settings()
        if not (settings.angel_enabled and settings.angel_options_enabled):
            return False
        if self._thread and self._thread.is_alive():
            return True

        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="chain-publisher", daemon=True)
        self._thread.start()
        log.info("option chain publisher started (floor %dms, channel %r)",
                 settings.angel_min_publish_ms, CHAIN_CHANNEL)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Idempotent, and safe on a publisher that never started."""
        self._stopping.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _interval(self) -> float:
        """The floor between publishes, in seconds.

        Read every cycle rather than captured at start, so the cadence can
        be retuned without a restart. Floored at 50ms: a zero here would
        spin the thread against Redis.
        """
        return max(get_settings().angel_min_publish_ms, 50) / 1000.0

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.publish_once()
            except Exception as exc:                      # noqa: BLE001
                # A publisher that dies takes the chain off every dashboard
                # while the feed underneath it keeps looking healthy. It
                # logs and carries on instead.
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("chain publish cycle failed: %s", exc)
            self._stopping.wait(self._interval())

    def publish_once(self) -> bool:
        """Build and fan out one chain, if there is a new one to send.

        Returns whether anything was published. Separated from the loop so
        the behaviour can be tested without a thread.
        """
        version = self._version()
        if version is not None and version == self._last_version:
            # No option tick has landed since the last publish. Re-sending
            # an identical chain would cost every open browser a redraw to
            # show it exactly what it already has.
            self.skipped_unchanged += 1
            return False

        # Imported here, not at module scope: the API layer reaches into
        # the workers, and importing it back at import time would close the
        # cycle.
        from ..api.market import live_chain

        payload = live_chain()
        if payload is None:
            # Still filling, gone quiet, or switched off. The HTTP endpoint
            # falls back to the polled chain in exactly this case, so the
            # dashboard is not left with nothing — it simply is not being
            # pushed yet.
            return False

        blob = json.dumps(payload, default=str)
        if not publish(CHAIN_CHANNEL, blob,
                       cache_key=CACHE_KEY, ttl=CACHE_TTL_SECONDS):
            self.failures += 1
            self.last_error = "redis publish returned false"
            return False

        self._last_version = version
        self.published += 1
        return True

    @staticmethod
    def _version() -> int | None:
        """A counter that changes exactly when the chain has new content.

        `None` means "cannot tell" — which publishes, rather than skipping.
        Sending a duplicate chain is a wasted redraw; skipping a real one
        freezes the panel.
        """
        try:
            from .option_chain_live import CHAIN
            return int(CHAIN.stats.ticks)
        except Exception:                                 # noqa: BLE001
            return None

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "channel": CHAIN_CHANNEL,
            "floor_ms": get_settings().angel_min_publish_ms,
            "published": self.published,
            "skipped_unchanged": self.skipped_unchanged,
            "failures": self.failures,
            "last_error": self.last_error,
        }


# One per process, like the feed and the scheduler.
PUBLISHER = ChainPublisher()


def start() -> bool:
    return PUBLISHER.start()


def stop() -> None:
    PUBLISHER.stop()


def status() -> dict:
    return PUBLISHER.status()
