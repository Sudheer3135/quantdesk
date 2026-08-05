"""Smart Money Concepts / ICT primitives.

Fair value gaps, order blocks, liquidity pools, and liquidity sweeps.
All of these are pattern definitions, not predictions. They tell you where
other people's orders are likely sitting, nothing more.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

import pandas as pd


@dataclass
class FairValueGap:
    index: int              # index of the middle (impulse) candle
    timestamp: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    filled: bool = False
    filled_index: int | None = None

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d["midpoint"] = self.midpoint
        d["size"] = self.size
        return d


@dataclass
class OrderBlock:
    index: int
    timestamp: pd.Timestamp
    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    mitigated: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class LiquidityPool:
    level: float
    side: Literal["buyside", "sellside"]
    touches: int
    last_index: int
    swept: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def find_fair_value_gaps(df: pd.DataFrame, min_atr_ratio: float = 0.15) -> list[FairValueGap]:
    """Three-candle imbalance.

    Bullish FVG: low[i+1] > high[i-1]  -> gap between them was never traded.
    Bearish FVG: high[i+1] < low[i-1]

    Gaps smaller than `min_atr_ratio` * ATR are noise and get dropped.
    A gap is marked filled once price trades back through its midpoint.
    """
    gaps: list[FairValueGap] = []
    if len(df) < 3:
        return gaps
    atr = df["atr14"] if "atr14" in df.columns else (df["high"] - df["low"]).rolling(14).mean()

    for i in range(1, len(df) - 1):
        prev_high, prev_low = df["high"].iloc[i - 1], df["low"].iloc[i - 1]
        next_high, next_low = df["high"].iloc[i + 1], df["low"].iloc[i + 1]
        threshold = float(atr.iloc[i] or 0) * min_atr_ratio

        if next_low > prev_high and (next_low - prev_high) > threshold:
            gaps.append(FairValueGap(i, df["timestamp"].iloc[i], "bullish",
                                     float(next_low), float(prev_high)))
        elif next_high < prev_low and (prev_low - next_high) > threshold:
            gaps.append(FairValueGap(i, df["timestamp"].iloc[i], "bearish",
                                     float(prev_low), float(next_high)))

    for gap in gaps:
        after = df.iloc[gap.index + 2 :]
        if after.empty:
            continue
        if gap.direction == "bullish":
            hit = after.index[after["low"] <= gap.midpoint]
        else:
            hit = after.index[after["high"] >= gap.midpoint]
        if len(hit):
            gap.filled = True
            gap.filled_index = int(hit[0])
    return gaps


def find_order_blocks(df: pd.DataFrame, impulse_atr: float = 1.2) -> list[OrderBlock]:
    """The last opposing candle before an impulsive move.

    Bullish OB: last down-candle before a strong up move.
    Bearish OB: last up-candle before a strong down move.
    "Strong" = the move covers more than `impulse_atr` * ATR.
    """
    blocks: list[OrderBlock] = []
    if len(df) < 3:
        return blocks
    atr = df["atr14"] if "atr14" in df.columns else (df["high"] - df["low"]).rolling(14).mean()

    for i in range(1, len(df) - 1):
        body = df["close"].iloc[i + 1] - df["open"].iloc[i + 1]
        limit = float(atr.iloc[i] or 0) * impulse_atr
        if limit <= 0:
            continue
        down_candle = df["close"].iloc[i] < df["open"].iloc[i]
        up_candle = df["close"].iloc[i] > df["open"].iloc[i]

        if body > limit and down_candle:
            blocks.append(OrderBlock(i, df["timestamp"].iloc[i], "bullish",
                                     float(df["high"].iloc[i]), float(df["low"].iloc[i])))
        elif -body > limit and up_candle:
            blocks.append(OrderBlock(i, df["timestamp"].iloc[i], "bearish",
                                     float(df["high"].iloc[i]), float(df["low"].iloc[i])))

    for ob in blocks:
        after = df.iloc[ob.index + 2 :]
        if after.empty:
            continue
        if ob.direction == "bullish":
            ob.mitigated = bool((after["low"] <= ob.top).any())
        else:
            ob.mitigated = bool((after["high"] >= ob.bottom).any())
    return blocks


def find_liquidity_pools(df: pd.DataFrame, tolerance_atr: float = 0.12,
                         lookback: int = 120) -> list[LiquidityPool]:
    """Equal highs and equal lows — where stop orders cluster.

    Two or more swing points within `tolerance_atr` * ATR of each other
    count as one pool. Buyside liquidity sits above equal highs, sellside
    below equal lows.
    """
    from .structure import find_swings

    window = df.iloc[-lookback:] if len(df) > lookback else df
    if window.empty:
        return []
    offset = len(df) - len(window)
    atr_val = float(window["atr14"].iloc[-1]) if "atr14" in window.columns else \
        float((window["high"] - window["low"]).mean())
    tol = max(atr_val * tolerance_atr, 1e-9)

    swings = find_swings(window.reset_index(drop=True), lookback=2)
    pools: list[LiquidityPool] = []

    for side, kind in (("buyside", "high"), ("sellside", "low")):
        levels = [s for s in swings if s.kind == kind]
        used = [False] * len(levels)
        for a in range(len(levels)):
            if used[a]:
                continue
            group = [levels[a]]
            used[a] = True
            for b in range(a + 1, len(levels)):
                if not used[b] and abs(levels[b].price - levels[a].price) <= tol:
                    group.append(levels[b])
                    used[b] = True
            if len(group) >= 2:
                level = sum(g.price for g in group) / len(group)
                last_idx = max(g.index for g in group) + offset
                after = df.iloc[last_idx + 1 :]
                swept = bool(
                    (after["high"] > level + tol).any() if side == "buyside"
                    else (after["low"] < level - tol).any()
                ) if not after.empty else False
                pools.append(LiquidityPool(level, side, len(group), last_idx, swept))

    return sorted(pools, key=lambda p: -p.touches)


def detect_sweep(df: pd.DataFrame, pools: list[LiquidityPool], bars: int = 5) -> dict | None:
    """Did the last few candles run a pool and close back inside?

    That rejection is the classic stop-hunt signature: wick through the
    level, close back on the original side.
    """
    if df.empty or not pools:
        return None
    recent = df.iloc[-bars:]
    for pool in pools:
        if pool.side == "buyside":
            poked = (recent["high"] > pool.level).any()
            rejected = float(recent["close"].iloc[-1]) < pool.level
            if poked and rejected:
                return {"side": "buyside", "level": pool.level, "bias": "bearish",
                        "note": "Buyside liquidity taken, price closed back below."}
        else:
            poked = (recent["low"] < pool.level).any()
            rejected = float(recent["close"].iloc[-1]) > pool.level
            if poked and rejected:
                return {"side": "sellside", "level": pool.level, "bias": "bullish",
                        "note": "Sellside liquidity taken, price closed back above."}
    return None


def nearest_unfilled_gap(gaps: list[FairValueGap], price: float,
                         direction: str | None = None) -> FairValueGap | None:
    live = [g for g in gaps if not g.filled and (direction is None or g.direction == direction)]
    if not live:
        return None
    return min(live, key=lambda g: abs(g.midpoint - price))
