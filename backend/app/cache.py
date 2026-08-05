"""Redis cache. Falls back to a no-op dict if Redis is unreachable so the
API still boots in a broken environment instead of crashing."""
import json
import logging
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)
_client = None


def client():
    global _client
    if _client is None:
        try:
            import redis
            _client = redis.from_url(get_settings().redis_url, decode_responses=True)
            _client.ping()
        except Exception as exc:
            log.warning("Redis unavailable (%s) — caching disabled.", exc)
            _client = False
    return _client or None


def get_json(key: str) -> Any | None:
    c = client()
    if not c:
        return None
    raw = c.get(key)
    return json.loads(raw) if raw else None


def set_json(key: str, value: Any, ttl: int = 60) -> None:
    c = client()
    if c:
        c.setex(key, ttl, json.dumps(value, default=str))
