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

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from ..cache import get_json
from ..config import get_settings
from ..data import regime_store
from ..db import SessionLocal, get_db
from ..market_hours import status as market_status
from ..risk import live as risk_live
from ..security import key_is_valid
from ..workers.ticker import classify_age

log = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])

SIGNAL_CHANNEL = "signals"
PRICE_CHANNEL = "prices"
# The streamed option chain rides the same socket as the price, so both
# feeds reach the dashboard on one connection and one cadence.
CHAIN_CHANNEL = "chain"
# Strategy v2's paper account. Published every second while a position is
# open, so its live P&L moves with the premium rather than on a poll.
V2_CHANNEL = "v2"
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
                await pubsub.subscribe(
                    SIGNAL_CHANNEL, PRICE_CHANNEL, CHAIN_CHANNEL, V2_CHANNEL)
                log.info("live stream subscribed to %r, %r, %r and %r",
                         SIGNAL_CHANNEL, PRICE_CHANNEL, CHAIN_CHANNEL, V2_CHANNEL)

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
                    elif message.get("channel") == CHAIN_CHANNEL:
                        # Tagged separately so the dashboard repaints the
                        # chain without redrawing the analysis around it.
                        await self.broadcast({"type": "chain", "chain": payload})
                    elif message.get("channel") == V2_CHANNEL:
                        await self.broadcast({"type": "v2", "v2": payload})
                    else:
                        await self.broadcast({
                            "type": "signal",
                            "signal": risk_live.ensure(payload),
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


async def _current_risk(signal_payload: dict | None) -> dict | None:
    """Today's risk verdict for the levels in a published signal.

    Off the event loop: the journal read is synchronous SQLAlchemy, and
    blocking the loop would stall every other socket on this process.

    Never raises. A database that is briefly unavailable should cost the
    desk its "risk now" line, not its price feed — and a missing verdict is
    rendered as missing rather than as approval.
    """
    if not signal_payload:
        return None

    def read() -> dict | None:
        with SessionLocal() as db:
            return risk_live.current(db, signal_payload)

    try:
        return await asyncio.to_thread(read)
    except Exception as exc:
        log.warning("could not compute current risk: %s", exc)
        return None


async def _current_regime() -> dict | None:
    """The market condition the desk is currently reading.

    Served from the stored table rather than recomputed here, deliberately.
    A second classification in this process would be free to disagree with
    the one the research split is built on — the dashboard would say RANGE
    while the table that decides which conditions the strategy is allowed to
    trade said TREND_UP, and nothing would report the contradiction.

    Off the event loop and never raising, same as `_current_risk`: a regime
    is a caption, and losing it must not cost the desk its price feed.
    """
    def read() -> dict | None:
        with SessionLocal() as db:
            s = get_settings()
            return regime_store.latest(db, s.watch_symbol, s.watch_timeframe)

    try:
        return await asyncio.to_thread(read)
    except Exception as exc:
        log.warning("could not read current regime: %s", exc)
        return None


@router.websocket("/ws/signals")
async def stream_signals(ws: WebSocket, key: str | None = Query(default=None)) -> None:
    # Browsers cannot set headers on a websocket handshake, so the key comes
    # as a query parameter here rather than in X-API-Key. The socket is
    # read-only, so this is about not broadcasting the desk to the network,
    # not about protecting a write.
    #
    # 1008 is "policy violation" — the close code a client can act on,
    # rather than dropping the connection and looking like a network fault.
    if not key_is_valid(key):
        await ws.close(code=1008, reason="invalid or missing key")
        return
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
        # A cached signal can outlive the deploy that started attaching risk
        # decisions. `ensure` labels one that has none rather than letting it
        # through unmarked — see audit finding H-4.
        snapshot_signal = risk_live.ensure(get_json("signal:latest"))
        await ws.send_json({
            "type": "snapshot",
            "signal": snapshot_signal,
            "price": snapshot_price,
            # A browser connecting between publishes gets the last streamed
            # chain rather than an empty panel it has to poll to fill.
            "chain": get_json("chain:stream:latest"),
            "v2": get_json("v2:latest"),
            "market": market_status(),
            # The signal's own verdict was true when it was published, which
            # may have been fifteen minutes ago — the cache TTL. The journal
            # moves in between: open a position and the old verdict is stale
            # without anything on screen saying so. This is the same question
            # asked of today's journal, now.
            "risk_now": await _current_risk(snapshot_signal),
            # The condition the analysis is being formed in. Sent on connect
            # and refreshed on the heartbeat rather than with the signal,
            # because a regime is a property of the market and keeps moving
            # between agent ticks.
            "regime": await _current_regime(),
        })

        while True:
            # A periodic beat keeps proxies from closing an idle socket and
            # lets the dashboard show a live clock between signals.
            await asyncio.sleep(HEARTBEAT_SECONDS)
            # Risk state changes when a trade is recorded, not when a bar
            # closes, so it rides the heartbeat rather than the signal. Worst
            # case the desk's "risk now" is one beat old.
            await ws.send_json({
                "type": "heartbeat",
                "market": market_status(),
                "risk_now": await _current_risk(get_json("signal:latest")),
                "regime": await _current_regime(),
            })
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


@router.get("/market/regime")
def current_regime(db: Session = Depends(get_db)) -> dict:
    """The latest classified bar, for clients that cannot use a socket.

    The dashboard falls back to polling whenever the socket is unavailable,
    and a regime that vanished in that state would read as "no condition"
    rather than "no connection".
    """
    s = get_settings()
    found = regime_store.latest(db, s.watch_symbol, s.watch_timeframe)
    if found is None:
        # Same shape either way. A caller that has to branch on which keys
        # came back will eventually forget to, and the branch it forgets is
        # the empty one.
        return {"timestamp": None, "session_date": None,
                "engine_version": None, "day": None, "hour": None,
                "note": "No regimes classified yet — "
                        "POST /data/regimes/backfill to build the history."}
    return found


@router.get("/market/status")
def status() -> dict:
    """Same information over plain HTTP, for clients that cannot use a socket."""
    return market_status()
