"""The live India VIX, from the Angel socket.

A separate store rather than a second price channel: VIX is an input to a
gate, read when a decision is made, not a tape anyone watches tick. It is
cached in Redis so the API and the paper trader read the same number, and
its age travels with it — a VIX reading from before a spike is precisely
the stale input that gate exists to catch.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from ..cache import set_json

log = logging.getLogger(__name__)

CACHE_KEY = "vix:latest"
CACHE_TTL_SECONDS = 900
# VIX prints about once a second. Writing every print to Redis buys nothing
# a gate read every few minutes could use.
PUBLISH_EVERY_SECONDS = 1.0

# A reading older than this is not "the VIX now" during a session.
MAX_AGE_SECONDS = 120.0


@dataclass
class VixReading:
    value: float
    source_time: datetime
    received_at: datetime

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.source_time).total_seconds())

    def to_dict(self, now: datetime | None = None) -> dict:
        moment = now or datetime.now(UTC)
        return {"value": self.value, "source": "angel",
                "source_time": self.source_time.isoformat(),
                "received_at": self.received_at.isoformat(),
                "age_seconds": round(self.age_seconds(moment), 2)}


class VixStore:
    def __init__(self, *, clock=None, publish=None) -> None:
        self._lock = threading.Lock()
        self._latest: VixReading | None = None
        self._published_at: datetime | None = None
        self._now = clock or (lambda: datetime.now(UTC))
        self._publish = publish or (lambda blob: set_json(CACHE_KEY, blob,
                                                          ttl=CACHE_TTL_SECONDS))
        self.ticks = 0

    def update(self, tick) -> None:
        """Fold one decoded LTP tick for the VIX token. Never raises."""
        received = self._now()
        reading = VixReading(float(tick.price), tick.source_time, received)
        with self._lock:
            self._latest = reading
            self.ticks += 1
            due = (self._published_at is None or
                   (received - self._published_at).total_seconds() >= PUBLISH_EVERY_SECONDS)
            if due:
                self._published_at = received
        if due:
            try:
                self._publish(reading.to_dict(received))
            except Exception as exc:                      # noqa: BLE001
                log.debug("VIX cache write failed: %s", exc)

    def current(self, *, max_age_seconds: float = MAX_AGE_SECONDS) -> float | None:
        """The live value, or None when there is none fresh enough to use."""
        with self._lock:
            reading = self._latest
        if reading is None or reading.age_seconds(self._now()) > max_age_seconds:
            return None
        return reading.value

    def last(self) -> VixReading | None:
        with self._lock:
            return self._latest

    def status(self) -> dict:
        reading = self.last()
        return {"ticks": self.ticks,
                "latest": reading.to_dict(self._now()) if reading else None}


VIX = VixStore()
