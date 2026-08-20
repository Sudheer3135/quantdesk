"""Live signal stream.

The agent already publishes every signal to a Redis channel. Until now
nothing consumed it — the dashboard polled on a timer instead, which meant
most requests returned the same signal it already had, and a genuinely new
signal could sit unseen for up to a minute.

This endpoint subscribes to that channel once and fans each message out to
every connected browser. One Redis subscription serves all of them.

The stream is deliberately thin: it carries whatever the agent published
and adds a market-status heartbeat. It never computes a signal itself, so
there is exactly one code path producing signals and one producing them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..cache import get_json
from ..config import get_settings
from ..market_hours import status as market_status
from ..workers.ticker import classify_age

log = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])

SIGNAL_CHANNEL = "signals"
PRICE_CHANNEL = "prices"
HEARTBEAT_SECONDS = 20


class Hub:
    """Tracks connected clients and relays Redis messages to them.

    One background task holds the Redis subscription; sockets come and go
    underneath it. Started lazily on the first connection so a deployment
    with no dashboard open costs nothing.
    """

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._relay: asyncio.Task | None = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        if self._relay is None or self._relay.done():
            self._relay = asyncio.create_task(self._pump())

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def _pump(self) -> None:
        """Hold the Redis subscription and forward what arrives."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            log.warning("redis.asyncio unavailable — live stream disabled")
            return

        while self.clients:
            try:
                client = aioredis.from_url(get_settings().redis_url,
                                           decode_responses=True)
                pubsub = client.pubsub()
                await pubsub.subscribe(SIGNAL_CHANNEL, PRICE_CHANNEL)
                log.info("live stream subscribed to %r and %r",
                         SIGNAL_CHANNEL, PRICE_CHANNEL)

                async for message in pubsub.listen():
                    if not self.clients:
                        break
                    if message.get("type") != "message":
                        continue
                    try:
                        payload = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue

                    # Prices arrive every few seconds, signals every few
                    # minutes. Tagging them lets the dashboard update the
                    # ticker without redrawing the whole analysis.
                    if message.get("channel") == PRICE_CHANNEL:
                        await self.broadcast({"type": "price", "price": payload})
                    else:
                        await self.broadcast({"type": "signal", "signal": payload,
                                              "market": market_status()})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redis restarts, network blips — reconnect rather than
                # leaving every dashboard silently frozen.
                log.warning("live stream dropped (%s); retrying in 5s", exc)
                await asyncio.sleep(5)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()
                    await client.aclose()

        log.info("live stream idle — no clients connected")


hub = Hub()


@router.websocket("/ws/signals")
async def stream_signals(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        # Send the last known signal immediately so a browser that connects
        # between agent ticks is not staring at an empty screen for minutes.
        snapshot_price = get_json("price:latest")
        if snapshot_price:
            # Same reasoning as the HTTP endpoint: a browser connecting two
            # minutes after the last tick must be told the price is two
            # minutes old, not handed the age it had when it was published.
            snapshot_price = snapshot_price | _aged(snapshot_price.get("source_time"))
        await ws.send_json({
            "type": "snapshot",
            "signal": get_json("signal:latest"),
            "price": snapshot_price,
            "market": market_status(),
        })

        while True:
            # A periodic beat keeps proxies from closing an idle socket and
            # lets the dashboard show a live clock between signals.
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await ws.send_json({"type": "heartbeat", "market": market_status()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket closed: %s", exc)
    finally:
        hub.disconnect(ws)


@router.get("/market/price")
def latest_price() -> dict:
    """The most recent price the ticker published, aged as of *now*.

    The dashboard reads this on load and whenever the socket is unavailable,
    so it can show a price without waiting for the next tick.

    The age is recomputed here rather than served from the cached blob. The
    stored `age_seconds` was true at the moment of publishing and grows
    stale along with the price it describes — returning it verbatim would
    report a two-minute-old price as three seconds old, which is the exact
    failure this field exists to prevent.
    """
    cached = get_json("price:latest")
    if cached:
        return cached | _aged(cached.get("source_time"))
    return {"price": None, "note": "No price published yet — the ticker runs "
                                   "every few seconds while the market is open.",
            "market_open": market_status()["open"],
            "freshness": "unknown", "age_seconds": None}


def _aged(source_time: str | None) -> dict:
    """Age a published price against the current clock."""
    if not source_time:
        return {"age_seconds": None, "freshness": "unknown"}
    try:
        age = round(
            (datetime.now(UTC) - datetime.fromisoformat(source_time)).total_seconds(), 3)
    except (TypeError, ValueError):
        return {"age_seconds": None, "freshness": "unknown"}
    return {"age_seconds": age, "freshness": classify_age(age)}


@router.get("/market/status")
def status() -> dict:
    """Same information over plain HTTP, for clients that cannot use a socket."""
    return market_status()
