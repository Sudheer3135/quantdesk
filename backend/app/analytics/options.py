"""Option chain analytics.

Input is a normalised chain: one row per strike with call and put fields.

    strike, call_oi, call_oi_change, call_volume, call_iv, call_ltp,
            put_oi,  put_oi_change,  put_volume,  put_iv,  put_ltp

The broker adapters are responsible for producing this shape, so the maths
below never has to care whose API the data came from.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

CHAIN_COLS = ["strike", "call_oi", "put_oi"]


@dataclass
class ChainSummary:
    spot: float
    atm_strike: float
    pcr_oi: float
    pcr_volume: float | None
    max_pain: float
    max_pain_distance_pct: float
    resistance_strikes: list[float]
    support_strikes: list[float]
    call_writing: list[float]
    put_writing: list[float]
    call_unwinding: list[float]
    put_unwinding: list[float]
    bias: str
    iv_skew: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate_chain(chain: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in CHAIN_COLS if c not in chain.columns]
    if missing:
        raise ValueError(f"option chain is missing columns: {missing}")
    out = chain.copy().sort_values("strike").reset_index(drop=True)
    for col in ["call_oi_change", "put_oi_change", "call_volume", "put_volume",
                "call_iv", "put_iv", "call_ltp", "put_ltp"]:
        if col not in out.columns:
            out[col] = 0.0
    return out.fillna(0.0)


def atm_strike(chain: pd.DataFrame, spot: float) -> float:
    return float(chain.loc[(chain["strike"] - spot).abs().idxmin(), "strike"])


def pcr(chain: pd.DataFrame, field: str = "oi") -> float | None:
    """Put-call ratio. Above ~1.2 leans bullish (heavy put writing),
    below ~0.7 leans bearish. It is a crowd-positioning gauge, not a signal."""
    calls = chain[f"call_{field}"].sum()
    puts = chain[f"put_{field}"].sum()
    if calls <= 0:
        return None
    return float(puts / calls)


def max_pain(chain: pd.DataFrame) -> float:
    """The strike where total option writer payout is smallest.

    For each candidate expiry price, sum what every open call and put would
    cost the writers. The minimum of that curve is max pain.
    """
    strikes = chain["strike"].values
    call_oi = chain["call_oi"].values
    put_oi = chain["put_oi"].values

    best_strike, best_pain = float(strikes[0]), None
    for expiry_price in strikes:
        call_pain = ((expiry_price - strikes).clip(min=0) * call_oi).sum()
        put_pain = ((strikes - expiry_price).clip(min=0) * put_oi).sum()
        total = call_pain + put_pain
        if best_pain is None or total < best_pain:
            best_pain, best_strike = total, float(expiry_price)
    return best_strike


def oi_levels(chain: pd.DataFrame, spot: float, top: int = 3) -> tuple[list[float], list[float]]:
    """Highest call OI above spot = resistance. Highest put OI below = support."""
    above = chain[chain["strike"] >= spot].nlargest(top, "call_oi")["strike"]
    below = chain[chain["strike"] <= spot].nlargest(top, "put_oi")["strike"]
    return sorted(float(s) for s in above), sorted((float(s) for s in below), reverse=True)


def buildup(chain: pd.DataFrame, spot: float, window: int = 5, top: int = 3):
    """Classify fresh positioning around the money.

    Rising OI = new positions. Call writing above spot caps upside;
    put writing below spot supports price. Unwinding is the reverse.
    """
    atm = atm_strike(chain, spot)
    step = chain["strike"].diff().median() or 50
    near = chain[(chain["strike"] - atm).abs() <= step * window]

    call_writing = near.nlargest(top, "call_oi_change")
    put_writing = near.nlargest(top, "put_oi_change")
    call_unwind = near.nsmallest(top, "call_oi_change")
    put_unwind = near.nsmallest(top, "put_oi_change")

    return (
        [float(s) for s in call_writing[call_writing["call_oi_change"] > 0]["strike"]],
        [float(s) for s in put_writing[put_writing["put_oi_change"] > 0]["strike"]],
        [float(s) for s in call_unwind[call_unwind["call_oi_change"] < 0]["strike"]],
        [float(s) for s in put_unwind[put_unwind["put_oi_change"] < 0]["strike"]],
    )


def iv_skew(chain: pd.DataFrame, spot: float, window: int = 3) -> float | None:
    """Put IV minus call IV near the money. Positive = fear priced in."""
    atm = atm_strike(chain, spot)
    step = chain["strike"].diff().median() or 50
    near = chain[(chain["strike"] - atm).abs() <= step * window]
    if near.empty or near["call_iv"].sum() == 0:
        return None
    return float(near["put_iv"].mean() - near["call_iv"].mean())


def summarise(chain: pd.DataFrame, spot: float) -> ChainSummary:
    c = validate_chain(chain)
    atm = atm_strike(c, spot)
    ratio = pcr(c, "oi") or 0.0
    mp = max_pain(c)
    resistance, support = oi_levels(c, spot)
    cw, pw, cu, pu = buildup(c, spot)

    # Bias is a plain reading of the crowd's positioning.
    score = 0
    if ratio >= 1.2:
        score += 1
    elif ratio <= 0.7:
        score -= 1
    if pw:
        score += 1
    if cw:
        score -= 1
    if spot > mp:
        score -= 1   # max pain pulls price back down toward it
    elif spot < mp:
        score += 1

    bias = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"

    return ChainSummary(
        spot=float(spot),
        atm_strike=atm,
        pcr_oi=round(ratio, 3),
        pcr_volume=round(pcr(c, "volume"), 3) if pcr(c, "volume") else None,
        max_pain=mp,
        max_pain_distance_pct=round((spot - mp) / spot * 100, 3) if spot else 0.0,
        resistance_strikes=resistance,
        support_strikes=support,
        call_writing=cw,
        put_writing=pw,
        call_unwinding=cu,
        put_unwinding=pu,
        bias=bias,
        iv_skew=iv_skew(c, spot),
    )
