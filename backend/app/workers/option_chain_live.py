"""The option chain, assembled from a websocket instead of a poll.

The polled chain is one HTTP request a minute against an endpoint that
throttles, and it arrives as a single snapshot up to sixty seconds old. This
assembles the same thing from SNAP_QUOTE ticks, which land in roughly four
hundred milliseconds — the difference between a chain that describes the
market and one that describes the market a minute ago.

**The chain is never a moment.** Each contract updates when it trades, so
at any instant the strikes hold prints of different ages, and a far strike
may not have traded for an hour. That is a property of the market, not a
defect, but it means a chain read off this store is a composite. Every
snapshot therefore carries `oldest_age_seconds` alongside it, and a
contract past `max_age` is dropped rather than presented as current — a
stale premium in an OI table is invisible, and a fabricated chain is worse
than no chain.

What this deliberately does not do is compute implied volatility. The feed
carries none, and deriving it here would put a modelled number in the same
frame as observed ones with nothing to tell them apart — the exact mixing
the option-buying module refuses. `iv` stays absent; callers that need it
already own a pricing model and can say so.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from ..data.option_universe import CALL, PUT, Contract, Universe

log = logging.getLogger(__name__)

# A contract with nothing newer than this is not reported. Generous, because
# a far strike legitimately goes quiet for long stretches — this is here to
# exclude yesterday's print, not to demand constant trading.
DEFAULT_MAX_AGE_SECONDS = 900.0

# The columns `analytics.options.summarise` validates against. One row per
# strike carrying both sides — not one row per contract.
CHAIN_COLUMNS = ("strike", "call_oi", "put_oi", "call_ltp", "put_ltp",
                 "call_volume", "put_volume", "call_bid", "call_ask",
                 "put_bid", "put_ask")


@dataclass
class Quote:
    """The latest state of one contract."""
    contract: Contract
    price: float
    open_interest: float | None
    volume: float | None
    bid: float | None
    ask: float | None
    source_time: datetime
    received_at: datetime
    updates: int = 0

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.source_time).total_seconds())


@dataclass
class ChainStats:
    ticks: int = 0
    unknown_token: int = 0
    contracts_seen: int = 0
    last_tick_at: datetime | None = None
    rebuilds: int = 0

    def to_dict(self) -> dict:
        return {
            "ticks": self.ticks,
            "unknown_token": self.unknown_token,
            "contracts_seen": self.contracts_seen,
            "rebuilds": self.rebuilds,
            "last_tick_at": (self.last_tick_at.isoformat()
                             if self.last_tick_at else None),
        }


class LiveChain:
    """Latest quote per contract, and the chain frame built from them.

    Written by the socket's reader thread and read by request handlers, so
    every mutation is under one lock. The lock is held only for dictionary
    work — never across a DataFrame build — because the reader thread
    stalling is the one thing that turns a slow request into a dropped tick.
    """

    def __init__(self, *, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
                 clock=None) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}
        self._universe: Universe | None = None
        self._by_token: dict[str, Contract] = {}
        self.max_age_seconds = max_age_seconds
        self.stats = ChainStats()
        self._now = clock or (lambda: datetime.now(UTC))

    # ---- universe -----------------------------------------------------

    def set_universe(self, universe: Universe) -> None:
        """Adopt a new set of contracts, discarding quotes that left it.

        Dropping the departed quotes matters: after a re-centre the old
        wings are no longer subscribed, so their last print would sit in the
        chain ageing quietly while looking exactly like a live one.
        """
        with self._lock:
            self._universe = universe
            self._by_token = universe.by_token()
            self._quotes = {t: q for t, q in self._quotes.items()
                            if t in self._by_token}
            self.stats.rebuilds += 1

    @property
    def universe(self) -> Universe | None:
        return self._universe

    def knows(self, token) -> bool:
        """Is this token part of the current universe?"""
        return str(token) in self._by_token

    # ---- writing ------------------------------------------------------

    def update(self, tick) -> bool:
        """Fold one decoded OptionTick into the chain.

        Returns whether it was applied. A tick for a token outside the
        universe is counted and dropped rather than stored: it is either a
        stale subscription from before a re-centre or somebody else's
        instrument, and neither belongs in this chain.
        """
        contract = self._by_token.get(str(tick.token))
        if contract is None:
            self.stats.unknown_token += 1
            return False

        received = self._now()
        with self._lock:
            existing = self._quotes.get(contract.token)
            self._quotes[contract.token] = Quote(
                contract=contract,
                price=tick.price,
                # A tick that carries no OI must not erase the last one we
                # had. Absent is not zero, and an OI table that blinks to
                # zero would move max-pain to a strike nobody is at.
                open_interest=(tick.open_interest if tick.open_interest is not None
                               else (existing.open_interest if existing else None)),
                volume=(tick.volume if tick.volume is not None
                        else (existing.volume if existing else None)),
                bid=tick.bid, ask=tick.ask,
                source_time=tick.source_time,
                received_at=received,
                updates=(existing.updates + 1) if existing else 1,
            )
            self.stats.ticks += 1
            self.stats.last_tick_at = received
            self.stats.contracts_seen = len(self._quotes)
        return True

    # ---- reading ------------------------------------------------------

    def snapshot(self, *, now: datetime | None = None) -> dict:
        """The chain as a frame, with the honesty attached.

        `frame` is empty until both sides of at least one strike have
        quoted. Half a strike is not a chain: `summarise` reads call and put
        open interest against each other, and a one-sided row would report a
        put/call ratio of infinity.
        """
        moment = now or self._now()
        with self._lock:
            quotes = list(self._quotes.values())

        fresh = [q for q in quotes
                 if q.age_seconds(moment) <= self.max_age_seconds]
        dropped = len(quotes) - len(fresh)

        rows: dict[float, dict] = {}
        for q in fresh:
            row = rows.setdefault(q.contract.strike, {
                "strike": q.contract.strike,
                "call_oi": 0.0, "put_oi": 0.0,
                "call_ltp": 0.0, "put_ltp": 0.0,
                "call_volume": 0.0, "put_volume": 0.0,
                "call_bid": 0.0, "call_ask": 0.0,
                "put_bid": 0.0, "put_ask": 0.0,
                "_sides": set(),
            })
            side = "call" if q.contract.option_type == CALL else "put"
            row[f"{side}_oi"] = float(q.open_interest or 0.0)
            row[f"{side}_ltp"] = float(q.price)
            row[f"{side}_volume"] = float(q.volume or 0.0)
            row[f"{side}_bid"] = float(q.bid or 0.0)
            row[f"{side}_ask"] = float(q.ask or 0.0)
            row["_sides"].add(q.contract.option_type)

        complete = [r for r in rows.values() if r["_sides"] == {CALL, PUT}]
        for r in complete:
            r.pop("_sides", None)
        complete.sort(key=lambda r: r["strike"])

        frame = (pd.DataFrame(complete, columns=list(CHAIN_COLUMNS))
                 if complete else pd.DataFrame(columns=list(CHAIN_COLUMNS)))
        if self._universe and self._universe.expiry:
            frame.attrs["expiry"] = self._universe.expiry.strftime("%d-%b-%Y")

        ages = [q.age_seconds(moment) for q in fresh]
        return {
            "frame": frame,
            "strikes": len(complete),
            "contracts": len(fresh),
            "one_sided": len(rows) - len(complete),
            "dropped_stale": dropped,
            "oldest_age_seconds": round(max(ages), 2) if ages else None,
            "newest_age_seconds": round(min(ages), 2) if ages else None,
            "expiry": (self._universe.expiry.isoformat()
                       if self._universe and self._universe.expiry else None),
            "at": moment.isoformat(),
        }

    def quotes(self, *, now: datetime | None = None) -> list[Quote]:
        """Every contract's latest quote that is still inside `max_age`.

        One entry per contract, not per strike, and one-sided strikes are
        kept: a buyer choosing a single call has no use for the put beside
        it, and dropping the call because its put has not printed would
        hide a perfectly tradable contract.
        """
        moment = now or self._now()
        with self._lock:
            quotes = list(self._quotes.values())
        return [q for q in quotes if q.age_seconds(moment) <= self.max_age_seconds]

    def status(self) -> dict:
        snap = self.snapshot()
        return {
            "subscribed": len(self._by_token),
            "quoted": snap["contracts"],
            "strikes": snap["strikes"],
            "one_sided": snap["one_sided"],
            "dropped_stale": snap["dropped_stale"],
            "oldest_age_seconds": snap["oldest_age_seconds"],
            "expiry": snap["expiry"],
            "universe": self._universe.to_dict() if self._universe else None,
            "stats": self.stats.to_dict(),
        }

    @property
    def ready(self) -> bool:
        """Whether the chain is worth reading at all."""
        return self.snapshot()["strikes"] >= 3


CHAIN = LiveChain()

# Strategy v2's contracts, when they are not on the nearest expiry. v2 buys
# a weekly with at least two sessions left, so on the day before expiry and
# on expiry day it trades next week's contracts, which the main universe
# does not carry. A separate store rather than a second expiry inside CHAIN:
# CHAIN's frame is one row per strike, and folding two expiries into it
# would merge two different contracts' open interest into one number the
# signal engine reads.
V2_CHAIN = LiveChain()


class ListedExpiries:
    """The expiries Angel's master lists, as last read by the feed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._expiries: list = []
        self.updated_at: datetime | None = None

    def set(self, expiries) -> None:
        with self._lock:
            self._expiries = sorted(set(expiries))
            self.updated_at = datetime.now(UTC)

    def get(self) -> list:
        with self._lock:
            return list(self._expiries)


LISTED = ListedExpiries()


def chain_for(expiry) -> LiveChain | None:
    """Whichever live store is streaming this expiry, if either is."""
    for store in (CHAIN, V2_CHAIN):
        if store.universe is not None and store.universe.expiry == expiry:
            return store
    return None
