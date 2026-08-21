"""Redis cache and pub/sub, with a connection that heals itself.

Audit finding M-1: the previous version memoised `False` into the module
global the first time Redis was unreachable, and the guard it checked was
`if _client is None`. That is never true again, so a thirty-second Redis
restart disabled caching for the lifetime of the process. The price ticker
kept polling and silently discarded every result, and a single log line was
the only trace.

The shape now: a failure schedules a retry instead of latching the feature
off, with exponential backoff so a genuinely dead Redis is not hammered once
per tick. A command that fails on a live handle drops it, which sends the
next call back through the same retry path — so a server that goes away
mid-session recovers on its own too.

Falling back to no cache is still the behaviour when Redis is down: the API
boots and serves in a broken environment rather than refusing to start. It
just no longer treats one bad moment as permanent.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

# Wait this long after a failure before trying again, doubling up to the
# ceiling. The floor is short enough that a Redis restart is picked up within
# a tick or two; the ceiling stops a long outage becoming a retry storm.
RETRY_MIN_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0

_client = None          # a live handle, or None
_next_attempt = 0.0     # monotonic deadline before which we do not retry
_backoff = 0.0          # current wait, doubling on each consecutive failure


def _schedule_retry(exc: Exception) -> None:
    global _next_attempt, _backoff
    _backoff = RETRY_MIN_SECONDS if not _backoff else min(_backoff * 2, RETRY_MAX_SECONDS)
    _next_attempt = time.monotonic() + _backoff
    log.warning("Redis unavailable (%s) — caching off, retrying in %.0fs.", exc, _backoff)


def client():
    """A live Redis handle, or None while it is unreachable.

    Never raises. Callers treat None as "no cache this time" and carry on.
    """
    global _client, _next_attempt, _backoff

    if _client is not None:
        return _client

    if time.monotonic() < _next_attempt:
        return None                      # still inside the backoff window

    try:
        import redis
        candidate = redis.from_url(get_settings().redis_url, decode_responses=True)
        candidate.ping()
    except Exception as exc:
        _schedule_retry(exc)
        return None

    if _backoff:
        log.info("Redis reconnected.")
    _client, _backoff, _next_attempt = candidate, 0.0, 0.0
    return _client


def drop(exc: Exception | None = None) -> None:
    """Discard the current handle after a failed command.

    Without this, a Redis that dies *after* a successful connection leaves a
    handle that raises on every use and nothing ever rebuilds it.
    """
    global _client
    if _client is not None:
        _client = None
        _schedule_retry(exc or RuntimeError("connection lost"))


def reset() -> None:
    """Forget all connection state. For tests, and for a deliberate re-read."""
    global _client, _next_attempt, _backoff
    _client, _next_attempt, _backoff = None, 0.0, 0.0


def get_json(key: str) -> Any | None:
    c = client()
    if not c:
        return None
    try:
        raw = c.get(key)
    except Exception as exc:
        drop(exc)
        return None
    return json.loads(raw) if raw else None


def set_json(key: str, value: Any, ttl: int = 60) -> None:
    c = client()
    if not c:
        return
    try:
        c.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:
        drop(exc)


def publish(channel: str, blob: str, cache_key: str | None = None,
            ttl: int = 120) -> bool:
    """Store the latest value and fan it out in one step.

    Used by the price ticker and the agent, which both need "remember this,
    then tell everyone". Returns whether it reached Redis, and never raises —
    a publish failure must not kill the scheduled job that called it, or one
    Redis blip ends the session's data collection.
    """
    c = client()
    if not c:
        return False
    try:
        if cache_key:
            c.setex(cache_key, ttl, blob)
        c.publish(channel, blob)
        return True
    except Exception as exc:
        drop(exc)
        return False
