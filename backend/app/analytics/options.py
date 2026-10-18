"""Option chain analytics.

Input is a normalised chain: one row per strike with call and put fields.

    strike, call_oi, call_oi_change, call_volume, call_iv, call_ltp,
            put_oi,  put_oi_change,  put_volume,  put_iv,  put_ltp

The broker adapters are responsible for producing this shape, so the maths
below never has to care whose API the data came from.

**Missing is not zero (OC-5).** An open-interest field the source did not
send is NaN here and stays NaN. It used to be filled with 0.0 on the way in,
which made a chain with no call OI read as a put/call ratio of 0.0 — deeply
bearish — and a chain with no OI at all put max pain on the lowest strike,
another bearish vote. Neither was a reading of the market. Now:

    OI missing on a side             → OI-based readings unavailable
    PCR with a side wholly missing    → unavailable, not 0, not neutral
    some strikes missing a side       → computed over the strikes that have
                                        both, and the coverage is reported
    a recorded zero                   → a real zero, counted as one

An unavailable reading is never translated into bullish, bearish or neutral.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

CHAIN_COLS = ["strike", "call_oi", "put_oi"]


# How complete the chain's open interest is.
OI_AVAILABLE = "available"      # both sides recorded on every strike
OI_PARTIAL = "partial"          # some strikes lack a side; paired ones used
OI_UNAVAILABLE = "unavailable"  # no strike has both sides recorded

# What the bias field says when there is no OI to read a bias from. Not one
# of bullish / bearish / neutral, on purpose.
BIAS_UNAVAILABLE = "unavailable"


@dataclass
class ChainSummary:
    spot: float
    atm_strike: float
    pcr_oi: float | None
    pcr_volume: float | None
    max_pain: float | None
    max_pain_distance_pct: float | None
    resistance_strikes: list[float]
    support_strikes: list[float]
    call_writing: list[float]
    put_writing: list[float]
    call_unwinding: list[float]
    put_unwinding: list[float]
    bias: str
    iv_skew: float | None = None
    oi_status: str = OI_AVAILABLE
    # Strikes with both sides' OI recorded, and strikes in the chain.
    oi_paired_strikes: int | None = None
    oi_total_strikes: int | None = None
    # Why PCR is None when it is: "missing" (no data) or "zero_call_oi"
    # (recorded, and every call strike genuinely had none).
    pcr_status: str = "ok"

    @property
    def oi_available(self) -> bool:
        return self.oi_status != OI_UNAVAILABLE

    def to_dict(self) -> dict:
        return asdict(self)


OPTIONAL_COLS = ["call_oi_change", "put_oi_change", "call_volume", "put_volume",
                 "call_iv", "put_iv", "call_ltp", "put_ltp"]


def validate_chain(chain: pd.DataFrame) -> pd.DataFrame:
    """Sorted, numeric, and with every absent value left absent.

    A column the source did not send is added as NaN, never 0.0, and no
    existing NaN is filled. Filling here is what turned a missing OI into
    a bearish put/call ratio.
    """
    missing = [c for c in CHAIN_COLS if c not in chain.columns]
    if missing:
        raise ValueError(f"option chain is missing columns: {missing}")
    out = chain.copy().sort_values("strike").reset_index(drop=True)
    for col in OPTIONAL_COLS:
        if col not in out.columns:
            out[col] = float("nan")
    for col in ["strike", *CHAIN_COLS[1:], *OPTIONAL_COLS]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def paired_oi(chain: pd.DataFrame) -> pd.DataFrame:
    """The strikes whose call *and* put OI were both recorded."""
    return chain[chain["call_oi"].notna() & chain["put_oi"].notna()]


def oi_status(chain: pd.DataFrame) -> str:
    paired = len(paired_oi(chain))
    if paired == 0:
        return OI_UNAVAILABLE
    return OI_AVAILABLE if paired == len(chain) else OI_PARTIAL


def atm_strike(chain: pd.DataFrame, spot: float) -> float:
    return float(chain.loc[(chain["strike"] - spot).abs().idxmin(), "strike"])


def pcr(chain: pd.DataFrame, field: str = "oi") -> float | None:
    """Put-call ratio. Above ~1.2 leans bullish (heavy put writing),
    below ~0.7 leans bearish. It is a crowd-positioning gauge, not a signal.

    Over strikes where both sides were recorded, so a strike missing its
    put cannot shrink the numerator while its call inflates the
    denominator. None when there is no such strike, or when the recorded
    call total is genuinely zero and the ratio is undefined.
    """
    both = chain[chain[f"call_{field}"].notna() & chain[f"put_{field}"].notna()]
    if both.empty:
        return None
    calls = float(both[f"call_{field}"].sum())
    puts = float(both[f"put_{field}"].sum())
    if calls <= 0:
        return None
    return puts / calls


def max_pain(chain: pd.DataFrame) -> float | None:
    """The strike where total option writer payout is smallest.

    For each candidate expiry price, sum what every open call and put would
    cost the writers. The minimum of that curve is max pain.

    Over strikes with both sides recorded. With none, there is no max pain:
    a chain of all-zero or all-missing OI used to return the lowest strike,
    which put "max pain" far below spot and voted bearish on nothing.
    """
    chain = paired_oi(chain)
    if chain.empty:
        return None
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
    """Highest call OI above spot = resistance. Highest put OI below = support.

    Only strikes with a recorded OI are ranked. The filter is explicit rather
    than left to `nlargest`: asked for more rows than hold a number, it pads
    the answer with NaN rows, so a chain with no OI at all used to come back
    with walls. With fewer recorded strikes than `top`, fewer levels are
    returned. A recorded zero is an observation and is ranked like any other.
    """
    def ranked(side: pd.DataFrame, column: str) -> pd.Series:
        oi = pd.to_numeric(side[column], errors="coerce")
        observed = side[oi.notna()].assign(**{column: oi[oi.notna()]})
        return observed.nlargest(top, column)["strike"]

    above = ranked(chain[chain["strike"] >= spot], "call_oi")
    below = ranked(chain[chain["strike"] <= spot], "put_oi")
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
    if near.empty or not (near["call_iv"].fillna(0) > 0).any() \
            or not (near["put_iv"].fillna(0) > 0).any():
        return None
    return float(near["put_iv"].mean() - near["call_iv"].mean())


def summarise(chain: pd.DataFrame, spot: float) -> ChainSummary:
    c = validate_chain(chain)
    atm = atm_strike(c, spot)
    status = oi_status(c)
    paired = paired_oi(c)

    ratio = pcr(c, "oi")
    if ratio is not None:
        pcr_state = "ok"
    elif status == OI_UNAVAILABLE:
        pcr_state = "missing"
    else:
        pcr_state = "zero_call_oi"
    mp = max_pain(c)
    resistance, support = oi_levels(c, spot)
    cw, pw, cu, pu = buildup(c, spot)
    volume_ratio = pcr(c, "volume")

    if status == OI_UNAVAILABLE:
        # No reading, so no vote. The check that consumes this is disabled
        # and says so; it is not scored as neutral.
        bias = BIAS_UNAVAILABLE
    else:
        # Bias is a plain reading of the crowd's positioning. Each component
        # votes only when it could be computed.
        score = 0
        if ratio is not None:
            if ratio >= 1.2:
                score += 1
            elif ratio <= 0.7:
                score -= 1
        if pw:
            score += 1
        if cw:
            score -= 1
        if mp is not None:
            if spot > mp:
                score -= 1   # max pain pulls price back down toward it
            elif spot < mp:
                score += 1
        bias = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"

    return ChainSummary(
        spot=float(spot),
        atm_strike=atm,
        pcr_oi=round(ratio, 3) if ratio is not None else None,
        pcr_volume=round(volume_ratio, 3) if volume_ratio else None,
        max_pain=mp,
        max_pain_distance_pct=(round((spot - mp) / spot * 100, 3)
                               if spot and mp is not None else None),
        resistance_strikes=resistance,
        support_strikes=support,
        call_writing=cw,
        put_writing=pw,
        call_unwinding=cu,
        put_unwinding=pu,
        bias=bias,
        iv_skew=iv_skew(c, spot),
        oi_status=status,
        oi_paired_strikes=int(len(paired)),
        oi_total_strikes=int(len(c)),
        pcr_status=pcr_state,
    )
