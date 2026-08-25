"""What kind of market this is.

The outcome study answered the first question — 167 in-session signals, 74%
of them stopped out first, average -0.66R — but not the useful follow-up:
*when* does the analysis fail? A win rate averaged over every condition the
market can be in is an average of things that should never have been added
together. A pullback rule that works in a trend and a mean-reversion rule
that works in a range will each look mediocre if you measure them across
both.

So this module answers one narrow question about any bar: what condition was
the market in at that moment. It produces a label, a confidence, and the
sentences that justify them. It is not a strategy. Nothing here emits a
direction to trade, sizes a position or overrides the risk manager.

Two levels, because they answer different questions:

  day   — the session so far, from the open onward, updating bar by bar.
          "Is today a trend day?" is a question about the whole session.
  hour  — a trailing hour. "Is this hour still trending?" can differ from
          the day's answer, and usually does at the turn.

Everything is causal. Every feature at bar *i* is computed from bars at or
before *i*: expanding sums inside the session, trailing windows, Wilder's
ATR. That is not a style preference — the point of this table is to split
past signal outcomes by the condition they were formed in, and a regime that
peeked at the next hour would make every one of those splits meaningless in
a way that looks like a discovery.

Dependency-free of the database on purpose, same as the rest of `analytics`:
it takes a candle frame and returns verdicts. Persistence lives in
`data.regime_store`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import indicators

# Bump when the maths changes. Stored on every row so a table holding two
# generations of the classifier is detectable rather than quietly mixed.
ENGINE_VERSION = "1.0"

TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
RANGE = "RANGE"
VOLATILE_CHOP = "VOLATILE_CHOP"
SQUEEZE = "SQUEEZE"

LABELS = (TREND_UP, TREND_DOWN, RANGE, VOLATILE_CHOP, SQUEEZE)

DAY, HOUR = "day", "hour"

# One hour on a 5-minute chart.
HOUR_BARS = 12

# The opening range: the first fifteen minutes. NSE's first three bars set
# the day's initial balance and a lot of the session trades against them.
OPENING_RANGE_BARS = 3

# ATR is compared against its own recent average rather than an absolute
# number, because "volatile" only means anything relative to this market's
# own habits. Roughly a week and a half of sessions.
ATR_BASELINE_BARS = 100
MIN_BASELINE_BARS = 20

# Below this many bars a verdict is provisional and says so. The day view is
# allowed to speak early — that is the point of a session-expanding read —
# but half an hour of data is not a day.
MATURE_DAY_BARS = 6
MATURE_HOUR_BARS = HOUR_BARS

# --------------------------------------------------------------------------
# thresholds
#
# Every one of these is a judgement, so they live together in one block where
# they can be argued with, rather than scattered as literals through the
# scoring. They were set from the shape of NIFTY 5-minute data, not fitted to
# outcomes — fitting them to the outcomes they are about to be used to
# explain is how you discover a pattern you built yourself.
# --------------------------------------------------------------------------

# Efficiency ratio: net displacement divided by the total path walked to get
# there. 1.0 is a straight line, 0.0 is a round trip back to the start.
EFFICIENCY_LO, EFFICIENCY_HI = 0.30, 0.60

# Net move, in ATRs, before direction counts as a lean.
DISPLACEMENT_LO, DISPLACEMENT_HI = 0.25, 1.50

# ATR against its own baseline.
EXPANSION_LO, EXPANSION_HI = 1.10, 1.70
SQUEEZE_HI, SQUEEZE_LO = 0.85, 0.60          # ratio falling: 0.85 -> 0, 0.60 -> 1

# Share of bars closing on one side of VWAP.
VWAP_SIDE_LO, VWAP_SIDE_HI = 0.55, 0.85

# VWAP crossings per bar. A third of bars flipping side is churn.
CHURN_LO, CHURN_HI = 0.10, 0.35

# Opening range measured in ATRs.
WIDE_RANGE_LO, WIDE_RANGE_HI = 1.20, 2.50
TIGHT_RANGE_HI, TIGHT_RANGE_LO = 1.00, 0.40

# Overnight gap in ATRs.
GAP_LO, GAP_HI = 0.50, 2.00

# Relative volume.
HEAVY_VOLUME_LO, HEAVY_VOLUME_HI = 1.10, 2.00


def ramp(x: float, lo: float, hi: float) -> float:
    """0 at or below `lo`, 1 at or above `hi`, straight line between.

    Used instead of a hard threshold everywhere. A cliff edge at 0.30
    efficiency would make 0.299 and 0.301 different kinds of market, and the
    resulting labels would flicker bar to bar on a market that had not
    changed.
    """
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return float(min(1.0, max(0.0, (x - lo) / (hi - lo))))


# The previous private names. Public because the entry-state layer grades its
# own thresholds the same way and a second implementation of "soft threshold"
# would be free to behave differently on the boundary.
_ramp = ramp


def ramp_down(x: float, hi: float, lo: float) -> float:
    """1 at or below `lo`, 0 at or above `hi`. The mirror of `ramp`."""
    return 1.0 - ramp(x, lo, hi)


_ramp_down = ramp_down


def _f(value) -> float | None:
    """A plain float, or None where pandas has NaN. Stored rows must be able
    to say "not measurable" rather than carrying a NaN into JSON."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(out) else out


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------

@dataclass
class Features:
    """What the classifier looked at. Stored alongside the label so a verdict
    can be re-argued later without recomputing the whole archive."""
    level: str
    bars: int = 0
    efficiency: float | None = None          # 0..1, straightness of the move
    displacement_atr: float | None = None    # signed, in ATRs
    atr_ratio: float | None = None           # ATR vs its own baseline
    vwap_side: float | None = None           # share of bars closing above VWAP
    vwap_crossings: float | None = None      # crossings per bar
    rvol: float | None = None                # None when volume is synthetic
    gap_atr: float | None = None             # day level only
    open_range_atr: float | None = None      # day level only

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    """One classification: what, how sure, and why."""
    level: str
    label: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    provisional: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _maturity(bars: int, level: str) -> float:
    full = MATURE_DAY_BARS if level == DAY else MATURE_HOUR_BARS
    return float(min(1.0, max(0.0, bars / full))) if full else 1.0


# What each level expects to be able to measure. Confidence is scaled by how
# much of this was actually available.
EXPECTED_FEATURES = ("efficiency", "displacement_atr", "atr_ratio",
                     "vwap_side", "vwap_crossings")
EXPECTED_DAY_EXTRA = ("open_range_atr",)


def _evidence(f: Features) -> float:
    """How much of what this level wanted to see it could actually see.

    Without this the classifier reports its most confident possible RANGE
    when it knows *nothing*: RANGE is scored as the residual — no direction,
    no expansion, no compression — and an unmeasurable feature contributes
    exactly the same zero as a measured feature saying "nothing is
    happening". A frame too short to have an ATR baseline therefore came out
    as RANGE at confidence 1.0, which is a black box wearing a number.

    Two omissions, both deliberate. `rvol` is not counted: the free source
    reports a constant for NIFTY volume, so counting it would permanently
    discount every verdict for a known property of the feed rather than for
    anything about the market. `gap_atr` is not counted either — the first
    session in an archive has no previous close, and that is not ignorance
    about the session, it is the edge of the data.
    """
    expected = EXPECTED_FEATURES + (EXPECTED_DAY_EXTRA if f.level == DAY else ())
    have = sum(1 for name in expected if getattr(f, name) is not None)
    return have / len(expected)


def classify(f: Features) -> Verdict:
    """Score every label, take the highest, explain the reasoning.

    Scoring rather than a decision tree, for one reason: a tree gives no
    honest confidence. Whichever branch you fall down, you are told the
    answer with equal certainty, and the interesting cases — a trend losing
    its efficiency, a range starting to expand — are exactly the ones sitting
    on a branch boundary. Scores let a near-tie report itself as a near-tie.
    """
    level = f.level
    reasons: list[str] = []

    # --- volatility, against this market's own recent habits ---------------
    if f.atr_ratio is None:
        expansion = compression = 0.0
        reasons.append(
            "ATR has no baseline yet (needs "
            f"{MIN_BASELINE_BARS} prior bars), so volatility is read as neutral.")
    else:
        expansion = _ramp(f.atr_ratio, EXPANSION_LO, EXPANSION_HI)
        compression = _ramp_down(f.atr_ratio, SQUEEZE_HI, SQUEEZE_LO)
        reasons.append(
            f"ATR is {f.atr_ratio:.2f}x its own {ATR_BASELINE_BARS}-bar average.")

    # --- how straight the move was ----------------------------------------
    if f.efficiency is None:
        trend_strength = 0.0
        reasons.append("Too few bars to measure directional efficiency.")
    else:
        trend_strength = _ramp(f.efficiency, EFFICIENCY_LO, EFFICIENCY_HI)
        reasons.append(
            f"Efficiency {f.efficiency:.2f} — {f.efficiency:.0%} of the distance "
            "travelled ended up as net direction.")

    # --- which way, and is VWAP agreeing ----------------------------------
    displacement = f.displacement_atr or 0.0
    up = _ramp(displacement, DISPLACEMENT_LO, DISPLACEMENT_HI)
    down = _ramp(-displacement, DISPLACEMENT_LO, DISPLACEMENT_HI)
    if f.displacement_atr is not None:
        reasons.append(
            f"Net move {displacement:+.2f} ATR over the window.")

    churn = 0.0
    if f.vwap_side is not None:
        # VWAP is the day's fair value. Holding one side of it is what
        # separates a trend from a market that is merely moving.
        up = 0.6 * up + 0.4 * _ramp(f.vwap_side, VWAP_SIDE_LO, VWAP_SIDE_HI)
        down = 0.6 * down + 0.4 * _ramp(1 - f.vwap_side, VWAP_SIDE_LO, VWAP_SIDE_HI)
        crossed = (f"crossing it {f.vwap_crossings:.2f} times per bar"
                   if f.vwap_crossings is not None else "")
        reasons.append(
            f"Closed above VWAP on {f.vwap_side:.0%} of the window's bars"
            + (f", {crossed}." if crossed else "."))
    if f.vwap_crossings is not None:
        churn = _ramp(f.vwap_crossings, CHURN_LO, CHURN_HI)

    scores = {
        # A trend needs to be straight and to lean. Compression works against
        # it, but does not veto it — a quiet, steady drift is still a trend.
        TREND_UP: trend_strength * up * (1 - 0.6 * compression),
        TREND_DOWN: trend_strength * down * (1 - 0.6 * compression),
        # Wide bars, or price flipping across VWAP, with nothing to show for
        # it. The (1 - trend_strength) factor is what stops a fast clean
        # trend from being called chop just because ATR expanded.
        VOLATILE_CHOP: max(expansion, 0.8 * churn) * (1 - trend_strength),
        # Compression, and only while the market is not going anywhere.
        SQUEEZE: compression * (1 - trend_strength),
        # The residual: no direction, no expansion, no compression. This is
        # the default a featureless market falls into, by construction.
        RANGE: (1 - trend_strength) * (1 - expansion) * (1 - compression),
    }

    if level == DAY:
        _apply_day_context(f, scores, reasons, trend_strength, up, down)

    if f.rvol is None:
        reasons.append(
            "Volume is unavailable or synthetic on this feed, so participation "
            "is not part of this read.")
    else:
        heavy = _ramp(f.rvol, HEAVY_VOLUME_LO, HEAVY_VOLUME_HI)
        scores[TREND_UP] += 0.10 * heavy * up * trend_strength
        scores[TREND_DOWN] += 0.10 * heavy * down * trend_strength
        scores[VOLATILE_CHOP] += 0.10 * heavy * (1 - trend_strength)
        reasons.append(f"Relative volume {f.rvol:.2f}x its 20-bar average.")

    scores = {k: round(float(min(1.0, max(0.0, v))), 4) for k, v in scores.items()}

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    label, top = ordered[0]
    runner_up = ordered[1][1]

    if top <= 0.0:
        # Nothing reached a threshold in either direction. Saying RANGE here
        # is a statement about the evidence, not about the market, and the
        # confidence says so.
        label, top, runner_up = RANGE, 0.0, 0.0
        reasons.append("No feature reached a threshold; this is an absence of "
                       "evidence rather than a positive reading.")

    separation = 0.0 if top <= 0 else (top - runner_up) / top
    maturity = _maturity(f.bars, level)
    evidence = _evidence(f)
    confidence = round(top * (0.6 + 0.4 * separation) * maturity * evidence, 3)

    if maturity < 1.0:
        reasons.append(
            f"Only {f.bars} bar(s) of {level} history — provisional, and the "
            "confidence is scaled down to match.")
    if evidence < 1.0:
        reasons.append(
            f"Only {evidence:.0%} of the expected inputs were measurable on "
            "this bar; the confidence is scaled down by the same share.")
    if evidence == 0.0:
        reasons.append("No feature reached a threshold; this is an absence of "
                       "evidence rather than a positive reading.")

    return Verdict(
        # Deduplicated, order preserved: the "absence of evidence" sentence
        # is reachable from two directions at once and saying it twice reads
        # like two findings.
        level=level, label=label, confidence=confidence,
        reasons=list(dict.fromkeys(reasons)),
        scores=scores, features=f.to_dict(), provisional=maturity < 1.0)


def _apply_day_context(f: Features, scores: dict, reasons: list[str],
                       trend_strength: float, up: float, down: float) -> None:
    """The two features only a session has: its opening range and its gap.

    Both are nudges rather than deciding votes. A wide opening range makes a
    volatile day more likely but does not make one; a gap only counts toward
    a trend if price is actually holding the gap's direction, which is why
    each term is multiplied by the directional bias already measured. A gap
    that has been filled contributes nothing, which is the correct answer.
    """
    if f.open_range_atr is not None:
        wide = _ramp(f.open_range_atr, WIDE_RANGE_LO, WIDE_RANGE_HI)
        tight = _ramp_down(f.open_range_atr, TIGHT_RANGE_HI, TIGHT_RANGE_LO)
        scores[VOLATILE_CHOP] += 0.25 * wide * (1 - trend_strength)
        scores[SQUEEZE] += 0.25 * tight * (1 - trend_strength)
        reasons.append(
            f"Opening 15 minutes spanned {f.open_range_atr:.2f} ATR.")

    if f.gap_atr is not None:
        scores[TREND_UP] += 0.15 * _ramp(f.gap_atr, GAP_LO, GAP_HI) * up
        scores[TREND_DOWN] += 0.15 * _ramp(-f.gap_atr, GAP_LO, GAP_HI) * down
        reasons.append(
            f"Opened {f.gap_atr:+.2f} ATR away from the previous close.")


# --------------------------------------------------------------------------
# turning a candle frame into features, causally
# --------------------------------------------------------------------------

def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Every feature for every bar, computed with past data only.

    Vectorised deliberately. The obvious implementation — slice the last N
    bars per row and measure them — is a Python loop over thousands of
    windows, and a backfill that takes a minute is a backfill nobody re-runs
    after changing a threshold. Every operation below is `cumsum`, `cummax`,
    `shift` or `rolling`, all of which look backwards only.
    """
    out = indicators.enrich(df)

    session = indicators.session_key(out)
    out["session_date"] = session
    bar_no = out.groupby(session).cumcount() + 1        # 1-based, within session
    out["bars_in_session"] = bar_no

    atr = out["atr14"].replace(0, np.nan)
    baseline = out["atr14"].rolling(
        ATR_BASELINE_BARS, min_periods=MIN_BASELINE_BARS).mean()
    out["atr_ratio"] = out["atr14"] / baseline.replace(0, np.nan)

    # --- path walked vs distance covered ----------------------------------
    step = out["close"].diff().abs()
    step_in_session = step.where(bar_no > 1, 0.0)       # do not span the overnight
    first_close = out.groupby(session)["close"].transform("first")

    path_day = step_in_session.groupby(session).cumsum()
    net_day = out["close"] - first_close
    out["efficiency_day"] = (net_day.abs() / path_day.replace(0, np.nan)).clip(0, 1)
    out["displacement_day"] = net_day / atr

    path_hour = step.rolling(HOUR_BARS - 1).sum()
    net_hour = out["close"] - out["close"].shift(HOUR_BARS - 1)
    out["efficiency_hour"] = (net_hour.abs() / path_hour.replace(0, np.nan)).clip(0, 1)
    out["displacement_hour"] = net_hour / atr

    # --- VWAP behaviour ----------------------------------------------------
    above = (out["close"] > out["vwap"]).astype(float)
    above = above.where(out["vwap"].notna())
    side = np.sign(out["close"] - out["vwap"])
    crossed = ((side != side.shift(1)) & (side != 0) & (side.shift(1) != 0))
    crossed = crossed.where(bar_no > 1, False).astype(float)

    out["vwap_side_day"] = above.groupby(session).cumsum() / bar_no
    out["vwap_cross_day"] = crossed.groupby(session).cumsum() / bar_no
    out["vwap_side_hour"] = above.rolling(HOUR_BARS).mean()
    out["vwap_cross_hour"] = crossed.rolling(HOUR_BARS).sum() / HOUR_BARS

    # --- participation -----------------------------------------------------
    # `relative_volume` is already all-NaN when the feed's volume is a
    # placeholder, which is the case for NIFTY on the free source. Nothing
    # below has to special-case it; None flows through to "not part of this
    # read" in the reasons.
    out["rvol_hour"] = out["rvol"]
    out["rvol_day"] = out["rvol"].groupby(session).transform(
        lambda s: s.expanding().mean())

    # --- opening range, frozen once the first fifteen minutes are done ----
    opening = bar_no <= OPENING_RANGE_BARS
    or_high = out["high"].groupby(session).cummax().where(opening)
    or_low = out["low"].groupby(session).cummin().where(opening)
    or_high = or_high.groupby(session).ffill()
    or_low = or_low.groupby(session).ffill()
    out["open_range_atr"] = (or_high - or_low) / atr

    # --- the overnight gap -------------------------------------------------
    by_session = out.groupby(session)
    prev_close = by_session["close"].last().shift(1)
    prev_atr = by_session["atr14"].last().shift(1).replace(0, np.nan)
    first_open = by_session["open"].transform("first")
    out["gap_atr"] = (first_open - session.map(prev_close)) / session.map(prev_atr)

    return out


def _features_at(row: pd.Series, level: str) -> Features:
    """Pull one bar's features out of the frame, at the requested level.

    Inside the first hour of a session there is no full trailing hour to
    look at, and the honest fallback is the session so far — which is the
    same window the day view is using. So early in the day the two levels
    agree by construction, and the reasons say why.
    """
    bars = int(row["bars_in_session"])
    common = {
        "atr_ratio": _f(row["atr_ratio"]),
        "rvol": _f(row["rvol_day"] if level == DAY else row["rvol_hour"]),
    }

    if level == DAY:
        return Features(
            level=DAY, bars=bars,
            efficiency=_f(row["efficiency_day"]),
            displacement_atr=_f(row["displacement_day"]),
            vwap_side=_f(row["vwap_side_day"]),
            vwap_crossings=_f(row["vwap_cross_day"]),
            gap_atr=_f(row["gap_atr"]),
            open_range_atr=_f(row["open_range_atr"]),
            **common)

    full_hour = bars >= HOUR_BARS
    return Features(
        level=HOUR, bars=min(bars, HOUR_BARS),
        efficiency=_f(row["efficiency_hour" if full_hour else "efficiency_day"]),
        displacement_atr=_f(
            row["displacement_hour" if full_hour else "displacement_day"]),
        vwap_side=_f(row["vwap_side_hour" if full_hour else "vwap_side_day"]),
        vwap_crossings=_f(
            row["vwap_cross_hour" if full_hour else "vwap_cross_day"]),
        **common)


def classify_frame(df: pd.DataFrame) -> pd.DataFrame:
    """A day verdict and an hour verdict for every bar in the frame.

    Returns a frame indexed the same way as the input, with the timestamp,
    the session date, and both verdicts as objects. Callers that want to
    store it go through `data.regime_store`.
    """
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "session_date", "day", "hour"])

    enriched = _feature_frame(df)
    rows = []
    for _, row in enriched.iterrows():
        rows.append({
            "timestamp": row["timestamp"],
            "session_date": row["session_date"],
            "day": classify(_features_at(row, DAY)),
            "hour": classify(_features_at(row, HOUR)),
        })
    return pd.DataFrame(rows)


def classify_latest(df: pd.DataFrame) -> dict | None:
    """Both verdicts for the most recent bar, ready to serialise.

    What the dashboard reads. None when there is nothing to classify, so a
    caller can render "no reading" rather than a fabricated RANGE.
    """
    if df.empty:
        return None
    enriched = _feature_frame(df)
    row = enriched.iloc[-1]
    return {
        "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
        "session_date": str(row["session_date"]),
        "engine_version": ENGINE_VERSION,
        "day": classify(_features_at(row, DAY)).to_dict(),
        "hour": classify(_features_at(row, HOUR)).to_dict(),
    }
