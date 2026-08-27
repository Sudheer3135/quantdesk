from fastapi import APIRouter

from ..cache import client as redis_client
from ..config import get_settings
from ..deps import get_broker
from ..workers import angel_feed, watchdog

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    s = get_settings()
    return {
        "status": "ok",
        "environment": s.environment,
        "broker": s.broker,
        "live_trading": s.live_trading,
        "redis": bool(redis_client()),
    }


@router.get("/health/broker")
def broker_health():
    broker = get_broker()
    try:
        quote = broker.quote(get_settings().watch_symbol)
        return {"broker": broker.name, "reachable": True, "last_price": quote["last_price"]}
    except Exception as exc:
        return {"broker": broker.name, "reachable": False, "error": str(exc)}


@router.get("/health/scheduler")
def scheduler_health():
    """Is the background work actually running?

    `/health` answers "is the process up", which stayed true throughout both
    sessions the desk lost. This answers the question that was actually
    failing: are the scheduled jobs completing, or is each run being skipped
    because the last one has not returned.

    Counts are since this process started and live in its memory, so a
    restart clears them. That is the right scope — this is a statement about
    the running desk, not about the archive.
    """
    return watchdog.report()


@router.get("/health/feed")
def feed_health():
    """Which source is serving the live price, and how well.

    Deliberately unauthenticated, like the rest of `/health` — and therefore
    deliberately free of anything secret. It reports counters, timestamps,
    a state name and latency percentiles. The Angel session appears only
    through `redacted`: enough to see *that* an account is connected and
    which one, never enough to be that account.

    `source` is the field to read first. It answers the question a desk
    actually has when a price looks wrong: am I looking at the push feed or
    at the poller that took over when it went quiet?
    """
    status = angel_feed.status()
    settings = get_settings()
    return {
        "live_price_source": status["source"],
        "transport": status["transport"],
        "fallback_broker": settings.broker,
        "angel": status,
    }
