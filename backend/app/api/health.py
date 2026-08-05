from fastapi import APIRouter

from ..cache import client as redis_client
from ..config import get_settings
from ..deps import get_broker

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
