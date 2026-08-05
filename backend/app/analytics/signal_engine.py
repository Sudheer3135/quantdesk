"""Signal engine.

Turns raw candles + an option chain into one decision: BUY, SELL or HOLD,
with a confidence score and a written reason for every point awarded.

Design rule: nothing here is a black box. Each check returns a score in
[-1, 1] and a sentence. Confidence is the weighted sum, normalised. If you
disagree with a signal you can read exactly which check caused it and
change that check's weight in `WEIGHTS`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from . import indicators, options, smc, structure

# Weights sum to 1.0. Tune these, then re-run the backtest before trusting them.
WEIGHTS: dict[str, float] = {
    "structure": 0.22,
    "vwap": 0.16,
    "trend": 0.14,
    "liquidity": 0.14,
    "fvg": 0.10,
    "option_chain": 0.16,
    "volume": 0.08,
}

# Below this, the engine returns HOLD no matter which way the score leans.
MIN_CONFIDENCE = 0.35


@dataclass
class Check:
    name: str
    score: float          # -1 bearish .. +1 bullish
    weight: float
    reason: str
    disabled: bool = False   # the input this check needs is unavailable

    @property
    def contribution(self) -> float:
        return 0.0 if self.disabled else self.score * self.weight

    def to_dict(self) -> dict:
        d = asdict(self)
        d["contribution"] = round(self.contribution, 4)
        return d


@dataclass
class Signal:
    symbol: str
    timeframe: str
    timestamp: str
    action: str                     # BUY | SELL | HOLD
    confidence: float               # 0..1
    price: float
    entry: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    checks: list[Check] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [c.to_dict() for c in self.checks]
        return d

    def explain(self) -> str:
        lines = [f"{self.action} {self.symbol} {self.timeframe} "
                 f"at {self.price:.2f} — confidence {self.confidence:.0%}"]
        for c in sorted(self.checks, key=lambda c: -abs(c.contribution)):
            if c.disabled:
                arrow = "off"
            else:
                arrow = "up" if c.score > 0 else "down" if c.score < 0 else "flat"
            lines.append(f"  [{arrow:>4}] {c.name}: {c.reason}")
        if self.stop_loss and self.target:
            lines.append(f"  Entry {self.entry:.2f} | Stop {self.stop_loss:.2f} | "
                         f"Target {self.target:.2f} | RR 1:{self.risk_reward:.2f}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------

# A structure break matters most on the bar it happens. Full weight while
# fresh, fading to nothing by STALE_BARS — a CHoCH from five hours ago that
# price has since traded back through is not evidence of anything.
FRESH_BARS = 12      # one hour on a 5m chart
STALE_BARS = 60      # five hours


def check_structure(state: structure.StructureState, current_index: int,
                    price: float) -> Check:
    ev = structure.last_event(state)
    if ev is None:
        return Check("structure", 0.0, WEIGHTS["structure"],
                     "No confirmed break of structure yet.")

    bars_ago = max(0, current_index - ev.index)
    if bars_ago >= STALE_BARS:
        return Check("structure", 0.0, WEIGHTS["structure"],
                     f"Last break was {bars_ago} bars ago — too old to trade on.",
                     disabled=True)

    decay = 1.0 if bars_ago <= FRESH_BARS else \
        1.0 - (bars_ago - FRESH_BARS) / (STALE_BARS - FRESH_BARS)

    direction = 1.0 if ev.direction == "bullish" else -1.0

    # A break price has since traded back through has failed, whatever the
    # label says. Reporting it at full strength is how you end up buying a
    # bullish CHoCH while price sits below the level it supposedly broke.
    reclaimed = (ev.direction == "bullish" and price < ev.broken_level) or \
                (ev.direction == "bearish" and price > ev.broken_level)
    if reclaimed:
        decay *= 0.3

    age = "this bar" if bars_ago == 0 else f"{bars_ago} bars ago"
    note = " — but price has traded back through it, so the break failed" \
        if reclaimed else ""

    if ev.kind == "CHOCH":
        return Check("structure", direction * 0.7 * decay, WEIGHTS["structure"],
                     f"CHoCH {ev.direction} {age}, broke {ev.broken_level:.2f}"
                     f"{note}.")
    return Check("structure", direction * decay, WEIGHTS["structure"],
                 f"BOS {ev.direction} {age}, cleared {ev.broken_level:.2f}"
                 f"{note}.")


def check_vwap(row: pd.Series) -> Check:
    price, vw = float(row["close"]), float(row["vwap"])
    if pd.isna(vw):
        return Check("vwap", 0.0, WEIGHTS["vwap"], "VWAP not available yet.")
    dist = (price - vw) / vw * 100
    if abs(dist) < 0.05:
        return Check("vwap", 0.0, WEIGHTS["vwap"],
                     f"Price sitting on VWAP ({vw:.2f}) — no edge either way.")
    upper, lower = float(row.get("vwap_upper", vw)), float(row.get("vwap_lower", vw))
    if price > upper:
        return Check(
            "vwap", 0.4, WEIGHTS["vwap"],
            f"Extended above the upper VWAP band ({upper:.2f}) — "
            f"buyers in control but stretched.")
    if price < lower:
        return Check(
            "vwap", -0.4, WEIGHTS["vwap"],
            f"Extended below the lower VWAP band ({lower:.2f}) — "
            f"sellers in control but stretched.")
    score = 1.0 if dist > 0 else -1.0
    side = "above" if dist > 0 else "below"
    return Check("vwap", score, WEIGHTS["vwap"],
                 f"Holding {side} VWAP ({vw:.2f}), {abs(dist):.2f}% away.")


def check_trend(row: pd.Series) -> Check:
    price = float(row["close"])
    e20, e50, e200 = float(row["ema20"]), float(row["ema50"]), float(row["ema200"])
    if pd.isna(e200):
        stacked_up = price > e20 > e50
        stacked_down = price < e20 < e50
    else:
        stacked_up = price > e20 > e50 > e200
        stacked_down = price < e20 < e50 < e200
    w = WEIGHTS["trend"]
    if stacked_up:
        return Check("trend", 1.0, w, "EMAs stacked up and price above all of them.")
    if stacked_down:
        return Check("trend", -1.0, w, "EMAs stacked down and price below all of them.")
    if price > e20 and price > e50:
        return Check("trend", 0.5, w, "Above the 20 and 50 EMA but not fully stacked.")
    if price < e20 and price < e50:
        return Check("trend", -0.5, w, "Below the 20 and 50 EMA but not fully stacked.")
    return Check("trend", 0.0, WEIGHTS["trend"], "EMAs tangled — no clean trend.")


def check_liquidity(sweep: dict | None, pools: list[smc.LiquidityPool], price: float) -> Check:
    if sweep:
        score = 1.0 if sweep["bias"] == "bullish" else -1.0
        return Check("liquidity", score, WEIGHTS["liquidity"],
                     f"{sweep['note']} Level {sweep['level']:.2f}.")
    live = [p for p in pools if not p.swept]
    if not live:
        return Check("liquidity", 0.0, WEIGHTS["liquidity"], "No untouched liquidity pools nearby.")
    nearest = min(live, key=lambda p: abs(p.level - price))
    gap_pct = abs(nearest.level - price) / price * 100
    if gap_pct > 0.4:
        return Check("liquidity", 0.0, WEIGHTS["liquidity"],
                     f"Nearest pool ({nearest.side}) at {nearest.level:.2f} is still far off.")
    # Price tends to get pulled into resting liquidity before reversing.
    score = 0.5 if nearest.side == "buyside" else -0.5
    return Check("liquidity", score, WEIGHTS["liquidity"],
                 f"Untouched {nearest.side} liquidity at {nearest.level:.2f} "
                 f"({nearest.touches} touches) is likely to attract price.")


# How close price must be before an unfilled gap is worth reacting to,
# measured in ATR so it scales with volatility instead of being a fixed
# number of points that means different things on different days.
FVG_REACH_ATR = 2.0


def check_fvg(gaps: list[smc.FairValueGap], price: float, atr: float) -> Check:
    live = [g for g in gaps if not g.filled]
    if not live:
        return Check("fvg", 0.0, WEIGHTS["fvg"], "No unfilled fair value gaps.")

    nearest = min(live, key=lambda g: abs(g.midpoint - price))
    inside = nearest.bottom <= price <= nearest.top
    distance = 0.0 if inside else min(abs(price - nearest.top),
                                      abs(price - nearest.bottom))

    # Without this gate a gap 300 points away scored the same as one price
    # was about to trade into, which quietly pushed every signal around.
    if not inside and atr > 0 and distance > atr * FVG_REACH_ATR:
        return Check("fvg", 0.0, WEIGHTS["fvg"],
                     f"Nearest unfilled {nearest.direction} FVG is "
                     f"{distance:.0f} points away ({distance / atr:.1f} ATR) "
                     f"— too far to matter.",
                     disabled=True)

    score = (1.0 if nearest.direction == "bullish" else -1.0) * (1.0 if inside else 0.4)
    where = "Trading inside" if inside else f"{distance:.0f} points from"
    return Check("fvg", score, WEIGHTS["fvg"],
                 f"{where} a {nearest.direction} FVG "
                 f"({nearest.bottom:.2f}-{nearest.top:.2f}).")


def check_option_chain(summary: options.ChainSummary | None) -> Check:
    if summary is None:
        return Check("option_chain", 0.0, WEIGHTS["option_chain"],
                     "No option chain available, so this check is disabled "
                     "and its weight is shared across the others.",
                     disabled=True)
    score = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}[summary.bias]
    bits = [f"PCR {summary.pcr_oi:.2f}", f"max pain {summary.max_pain:.0f}"]
    if summary.put_writing:
        bits.append(f"put writing at {summary.put_writing[0]:.0f}")
    if summary.call_writing:
        bits.append(f"call writing at {summary.call_writing[0]:.0f}")
    return Check("option_chain", score, WEIGHTS["option_chain"],
                 f"Chain reads {summary.bias}: " + ", ".join(bits) + ".")


def check_volume(row: pd.Series, volume_is_real: bool = True) -> Check:
    if not volume_is_real:
        return Check("volume", 0.0, WEIGHTS["volume"],
                     "Data source reports no volume, so this check is disabled "
                     "and its weight is shared across the others.",
                     disabled=True)
    rvol = float(row.get("rvol", float("nan")))
    if pd.isna(rvol):
        return Check("volume", 0.0, WEIGHTS["volume"],
                     "Not enough bars for relative volume.", disabled=True)
    direction = 1.0 if row["close"] >= row["open"] else -1.0
    if rvol >= 1.5:
        return Check("volume", direction, WEIGHTS["volume"],
                     f"Relative volume {rvol:.2f}x — real participation behind this candle.")
    if rvol <= 0.6:
        return Check("volume", 0.0, WEIGHTS["volume"],
                     f"Relative volume only {rvol:.2f}x — thin, treat breaks with suspicion.")
    return Check("volume", direction * 0.3, WEIGHTS["volume"], f"Average volume ({rvol:.2f}x).")


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

def generate(
    candles: pd.DataFrame,
    symbol: str = "NIFTY",
    timeframe: str = "5m",
    chain: pd.DataFrame | None = None,
    india_vix: float | None = None,
    rr_target: float = 2.0,
    atr_stop_multiple: float = 1.2,
) -> Signal:
    df = indicators.enrich(candles)
    if len(df) < 30:
        raise ValueError("need at least 30 candles to read structure reliably")

    row = df.iloc[-1]
    price = float(row["close"])

    state = structure.analyse(df)
    gaps = smc.find_fair_value_gaps(df)
    blocks = smc.find_order_blocks(df)
    pools = smc.find_liquidity_pools(df)
    sweep = smc.detect_sweep(df, pools)
    chain_summary = options.summarise(chain, price) if chain is not None else None

    checks = [
        check_structure(state, len(df) - 1, price),
        check_vwap(row),
        check_trend(row),
        check_liquidity(sweep, pools, price),
        check_fvg(gaps, price, float(row["atr14"])),
        check_option_chain(chain_summary),
        check_volume(row, volume_is_real=indicators.has_real_volume(df)),
    ]

    # A disabled check must not quietly shrink the scale. If two of seven
    # checks cannot run, the remaining five have to be able to reach full
    # confidence between them, otherwise the threshold silently gets
    # stricter and the engine just stops trading without saying why.
    live = [c for c in checks if not c.disabled]
    live_weight = sum(c.weight for c in live)
    if not live:
        raise ValueError("every check was disabled — no usable market data")
    scale = 1.0 / live_weight

    raw = sum(c.contribution for c in checks) * scale
    confidence = min(abs(raw), 1.0)

    # High VIX means wider noise; demand more agreement before acting.
    threshold = MIN_CONFIDENCE
    if india_vix is not None and india_vix > 20:
        threshold += 0.10

    if confidence < threshold:
        action = "HOLD"
    else:
        action = "BUY" if raw > 0 else "SELL"

    signal = Signal(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=row["timestamp"].isoformat(),
        action=action,
        confidence=round(confidence, 4),
        price=price,
        checks=checks,
        context={
            "trend": state.trend,
            "last_structure_event": structure.last_event(state).to_dict()
            if structure.last_event(state) else None,
            "unfilled_fvgs": [g.to_dict() for g in gaps if not g.filled][-5:],
            "order_blocks": [b.to_dict() for b in blocks if not b.mitigated][-5:],
            "liquidity_pools": [p.to_dict() for p in pools[:6]],
            "sweep": sweep,
            "option_chain": chain_summary.to_dict() if chain_summary else None,
            "india_vix": india_vix,
            "vwap": None if pd.isna(row["vwap"]) else float(row["vwap"]),
            "atr14": None if pd.isna(row["atr14"]) else float(row["atr14"]),
            "threshold_used": round(threshold, 3),
            "disabled_checks": [c.name for c in checks if c.disabled],
            "weight_scale": round(scale, 4),
        },
    )

    if action != "HOLD":
        atr_val = float(row["atr14"])
        risk = max(atr_val * atr_stop_multiple, price * 0.0008)
        if action == "BUY":
            signal.entry = price
            signal.stop_loss = round(price - risk, 2)
            signal.target = round(price + risk * rr_target, 2)
        else:
            signal.entry = price
            signal.stop_loss = round(price + risk, 2)
            signal.target = round(price - risk * rr_target, 2)
        signal.risk_reward = round(rr_target, 2)

    return signal