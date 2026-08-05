"""Market structure.

Swing points -> trend state -> break of structure (BOS) and change of
character (CHoCH).

Definitions used here (stated plainly so the code and the docs agree):

  Swing high   a bar whose high is the highest within `lookback` bars on
               both sides. Confirmed only after `lookback` bars have closed,
               so it is never repainted.
  Uptrend      the last two confirmed swing highs are rising AND the last
               two confirmed swing lows are rising.
  BOS          price closes beyond the most recent swing point *in the
               direction of the existing trend*. Continuation.
  CHoCH        price closes beyond the most recent swing point *against*
               the existing trend. First warning of a reversal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

import pandas as pd

Direction = Literal["bullish", "bearish", "range"]


@dataclass
class Swing:
    index: int
    timestamp: pd.Timestamp
    price: float
    kind: Literal["high", "low"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class StructureEvent:
    index: int
    timestamp: pd.Timestamp
    kind: Literal["BOS", "CHOCH"]
    direction: Literal["bullish", "bearish"]
    broken_level: float
    close: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class StructureState:
    trend: Direction = "range"
    swings: list[Swing] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    last_swing_high: Swing | None = None
    last_swing_low: Swing | None = None

    def to_dict(self) -> dict:
        return {
            "trend": self.trend,
            "swings": [s.to_dict() for s in self.swings[-12:]],
            "events": [e.to_dict() for e in self.events[-8:]],
            "last_swing_high": self.last_swing_high.to_dict() if self.last_swing_high else None,
            "last_swing_low": self.last_swing_low.to_dict() if self.last_swing_low else None,
        }


def find_swings(df: pd.DataFrame, lookback: int = 3) -> list[Swing]:
    """Fractal swings. A point is only emitted once `lookback` bars have
    closed after it, so results never change on the next tick."""
    swings: list[Swing] = []
    highs, lows = df["high"].values, df["low"].values
    n = len(df)
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        window_l = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_h.max() and (window_h.argmax() == lookback):
            swings.append(Swing(i, df["timestamp"].iloc[i], float(highs[i]), "high"))
        elif lows[i] == window_l.min() and (window_l.argmin() == lookback):
            swings.append(Swing(i, df["timestamp"].iloc[i], float(lows[i]), "low"))
    return swings


def analyse(df: pd.DataFrame, lookback: int = 3) -> StructureState:
    """Walk the candles forward and build the structure state."""
    state = StructureState()
    swings = find_swings(df, lookback)
    state.swings = swings
    if not swings:
        return state

    # A swing is only usable `lookback` bars after it printed.
    confirmed_at = {s.index: s.index + lookback for s in swings}
    pending = sorted(swings, key=lambda s: confirmed_at[s.index])
    p = 0

    recent_highs: list[float] = []
    recent_lows: list[float] = []
    broken_high: Swing | None = None
    broken_low: Swing | None = None

    for i in range(len(df)):
        close = float(df["close"].iloc[i])
        ts = df["timestamp"].iloc[i]

        # promote any swings that have become confirmed by this bar
        while p < len(pending) and confirmed_at[pending[p].index] <= i:
            s = pending[p]
            if s.kind == "high":
                state.last_swing_high = s
                broken_high = None
                recent_highs.append(s.price)
            else:
                state.last_swing_low = s
                broken_low = None
                recent_lows.append(s.price)
            p += 1

        # upward break
        sh = state.last_swing_high
        if sh and broken_high is not sh and close > sh.price:
            kind = "CHOCH" if state.trend == "bearish" else "BOS"
            state.events.append(StructureEvent(i, ts, kind, "bullish", sh.price, close))
            state.trend = "bullish"
            broken_high = sh

        # downward break
        sl = state.last_swing_low
        if sl and broken_low is not sl and close < sl.price:
            kind = "CHOCH" if state.trend == "bullish" else "BOS"
            state.events.append(StructureEvent(i, ts, kind, "bearish", sl.price, close))
            state.trend = "bearish"
            broken_low = sl

    # if no break has happened yet, fall back to swing sequencing
    if not state.events and len(recent_highs) >= 2 and len(recent_lows) >= 2:
        hh = recent_highs[-1] > recent_highs[-2]
        hl = recent_lows[-1] > recent_lows[-2]
        if hh and hl:
            state.trend = "bullish"
        elif not hh and not hl:
            state.trend = "bearish"

    return state


def last_event(state: StructureState) -> StructureEvent | None:
    return state.events[-1] if state.events else None
