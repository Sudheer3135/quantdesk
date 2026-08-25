"""Measuring the two layers apart from each other.

The reason for splitting the output was that direction and timing fail
independently, so measuring them together hides which one is broken. Step 1
showed what that looks like: eleven signals fired as BUY into an hour that
was already trending up, and their average maximum favourable excursion was
0.059R. Read as one number that is a losing strategy. Read as two, it is a
correct direction and a catastrophic entry, and only the second needs fixing.

So this reports two things that are never averaged together:

  **Bias accuracy** — did price go the way the higher timeframe pointed?
  A directional claim, judged on forward return, with no reference to entry,
  stop or target. A bias can be right while every trade taken on it loses.

  **Entry timing** — of the trades that were actually replayed, did the ones
  the model would have allowed behave differently from the ones it would have
  told the desk to wait on? Judged on what the trade did, with no reference
  to whether the direction was right.

Two things this is careful about, both of which would otherwise turn it into
a machine for producing flattering numbers:

  The plans are **recomputed, not remembered**. Rows written before the plan
  columns existed carry no bias, so every plan here is rebuilt from the
  archive at the signal's own bar, from a window the same size the live path
  actually sees. That is a study, and it is labelled one everywhere it
  appears — never written back into the historical rows as though the desk
  had said it at the time.

  Bias accuracy is reported **against the base rate**. If 58% of hours in
  this sample closed up, then a bias that is BULLISH every time scores 58%
  and knows nothing. The `edge_over_base_rate` field is the only number in
  the accuracy block worth reading on its own.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from ..analytics import plan as plan_builder
from ..backtest.feed import HistoricalFeed
from . import outcomes as outcome_study

log = logging.getLogger(__name__)

# How far ahead a bias is judged. One hour on a five-minute chart — the
# horizon the 15-minute and hourly readings are actually about. A bias
# judged on the next bar would be measuring noise; judged on the next day it
# would be measuring something the desk never claimed.
BIAS_HORIZON_BARS = 12

# Moves smaller than this are neither right nor wrong. Without it, a bias is
# scored correct for a two-point drift, and the accuracy figure becomes a
# coin flip dressed up as a measurement.
BIAS_FLAT_ATR = 0.25

# The window handed to each replayed plan. Deliberately the same size the
# live path sees — the agent fetches five sessions — so this reproduces the
# decision the desk would have made, not a better one made with more history.
# Structure swings genuinely depend on how far back you can see, so a longer
# window here would be measuring a system nobody is running.
PLAN_LOOKBACK_BARS = 375

# The excursion that decides whether an entry was well timed: did the trade
# reach half its risk in favour before it reached half against?
TIMING_R = 0.5

MIN_MEANINGFUL = 20


# --------------------------------------------------------------------------
# one signal
# --------------------------------------------------------------------------

@dataclass
class Replayed:
    """One stored signal, seen through the two-layer model."""
    signal_id: int
    signal_bar_time: str
    action: str                       # what the existing engine said
    confidence: float

    bias: str
    bias_confidence: float
    entry_state: str
    entry_confidence: float
    entry_style: str | None
    regime_day: str | None
    regime_hour: str | None

    # Did the bias's direction happen? Independent of any trade.
    forward_points: float | None = None
    forward_atr: float | None = None
    bias_verdict: str | None = None   # correct | wrong | flat | no-claim

    # What the trade did, carried over from the outcome study unchanged.
    outcome: str | None = None
    r_multiple: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    favour_first: bool | None = None

    @property
    def agrees_with_engine(self) -> bool | None:
        """Does the new bias point the same way as the old verdict?"""
        if self.bias == plan_builder.NEUTRAL:
            return None
        return ((self.action == "BUY" and self.bias == plan_builder.BULLISH)
                or (self.action == "SELL" and self.bias == plan_builder.BEARISH))

    def to_dict(self) -> dict:
        return asdict(self) | {"agrees_with_engine": self.agrees_with_engine}


CORRECT, WRONG, FLAT, NO_CLAIM = "correct", "wrong", "flat", "no-claim"


def _bias_verdict(bias: str, forward_atr: float | None) -> str:
    """Was the directional claim borne out?

    NEUTRAL makes no claim, so it cannot be right or wrong — counting it
    either way would let a model that never commits inflate or deflate its
    own score by staying quiet.
    """
    if bias == plan_builder.NEUTRAL:
        return NO_CLAIM
    if forward_atr is None:
        return NO_CLAIM
    if abs(forward_atr) < BIAS_FLAT_ATR:
        return FLAT
    went_up = forward_atr > 0
    wanted_up = bias == plan_builder.BULLISH
    return CORRECT if went_up == wanted_up else WRONG


def _forward_move(feed: HistoricalFeed, index: int,
                  horizon: int = BIAS_HORIZON_BARS) -> tuple[float | None, float | None]:
    """Points and ATRs from this bar's close to the close `horizon` bars on.

    Read through the feed's cursor like everything else here. Looking ahead
    at history is legitimate — both bars are in the past — but going through
    the same guard means this cannot accidentally read a bar the walk has not
    reached.

    **Bounded to one session.** The candle frame is contiguous in *rows*, not
    in time: bar N is 15:30 and bar N+1 is 09:15 the next morning, with an
    overnight gap and sometimes a weekend between them. Counting twelve rows
    forward from 14:00 therefore measured a real hour, while counting twelve
    forward from 15:00 measured an hour plus a night — a different quantity
    entirely, containing the gap open, which on this instrument is where a
    large share of the movement lives.

    That mattered: 19 of the 167 replayed signals (11.4%) had a horizon that
    crossed a close, one of them spanning Friday to Monday, and their forward
    returns were being averaged in with intraday ones as though they were the
    same measurement.

    A horizon that runs past the close is **unresolved**, not zero and not
    truncated. Both alternatives are worse than an honest gap: zero would
    score a bias as flat on evidence that does not exist, and a shortened
    horizon would quietly judge late-session bars on a different question
    than the rest. `None` propagates to `NO_CLAIM`, which `_accuracy` already
    excludes from both the numerator and the base rate.
    """
    target = index + horizon
    if target >= len(feed):
        return None, None
    feed.seek(target)
    # An NSE session opens and closes on one IST date and never spans
    # midnight, so the trading date *is* the session identity here. This
    # catches weekends and holidays for free — no calendar lookup needed,
    # because a gap of any length shows up as two different dates.
    if feed.ist(index).date() != feed.ist(target).date():
        return None, None
    here = float(feed.bar(index)["close"])
    later = float(feed.bar(target)["close"])
    # `HistoricalFeed` enriches on construction, so ATR is already there.
    atr = feed._frame["atr14"].iloc[index]                   # noqa: SLF001
    points = later - here
    if atr is None or pd.isna(atr) or atr <= 0:
        return round(points, 2), None
    return round(points, 2), round(points / float(atr), 3)


def _favour_first(feed: HistoricalFeed, entry_index: int, side: str,
                  entry: float, risk: float) -> bool | None:
    """Did the trade reach half its risk in favour before half against?

    The timing question stated so it can be answered from bars. A trade that
    immediately draws down half a stop before doing anything was entered too
    late or too early, whatever it eventually did — and averaging its final R
    with a trade that never went offside hides exactly that.
    """
    if risk <= 0:
        return None
    threshold = risk * TIMING_R
    want_up = side == "BUY"
    for j in range(entry_index, len(feed)):
        feed.seek(j)
        bar = feed.bar(j)
        favour = (float(bar["high"]) - entry if want_up
                  else entry - float(bar["low"]))
        against = (entry - float(bar["low"]) if want_up
                   else float(bar["high"]) - entry)
        hit_favour, hit_against = favour >= threshold, against >= threshold
        if hit_favour and hit_against:
            # One bar covering both. The pessimistic reading, matching the
            # outcome study's stop-first convention: five-minute bars do not
            # record which came first.
            return False
        if hit_favour:
            return True
        if hit_against:
            return False
    return None


# --------------------------------------------------------------------------
# the study
# --------------------------------------------------------------------------

@dataclass
class TwoLayerReport:
    symbol: str
    timeframe: str
    selection: dict
    candles: dict
    replayed: int = 0
    plan_failures: int = 0
    bias_accuracy: dict = field(default_factory=dict)
    bias_by_regime: list[dict] = field(default_factory=list)
    entry_states: list[dict] = field(default_factory=list)
    entry_timing: list[dict] = field(default_factory=list)
    engine_agreement: dict = field(default_factory=dict)
    reinterpretation: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


CAVEATS = [
    "The plans here were recomputed from the archive, not read from the "
    "signals table. Rows written before the plan columns existed carry no "
    "bias, and writing today's answer into a historical row would put a "
    "fabricated record into the audit trail. This is a study of what the "
    "model would have said, and nothing here is stored back.",
    "Each plan is rebuilt from a window the same size the live path sees "
    "(five sessions). Structure swings depend on how far back you can look, "
    "so a longer window would measure a system nobody is running.",
    "Bias accuracy is a directional claim measured on forward return over "
    "one hour. It is not trade success: a bias can be right while every "
    "trade taken on it is stopped out, which is precisely the failure the "
    "two layers exist to separate.",
    "Read `edge_over_base_rate`, not `accuracy`. If most hours in this "
    "sample closed up, a permanently bullish model scores well and knows "
    "nothing.",
    "Moves smaller than a quarter of an ATR are counted as flat rather than "
    "as a correct call.",
    "Entry-state counts here are what the model WOULD have said. No signal "
    "was gated on them; the gate is a later step, and this is the evidence "
    "it would be built on rather than a result it produced.",
    "Sample sizes are small and the observations are not independent. "
    "Consecutive signals within one move describe that move.",
]


def _bucket_rows(rows: list[Replayed], key) -> list[dict]:
    """Group replayed signals and report both layers per group."""
    groups: dict[str, list[Replayed]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)

    out = []
    for label, group in sorted(groups.items()):
        judged = [r for r in group if r.bias_verdict in (CORRECT, WRONG)]
        traded = [r for r in group if r.r_multiple is not None]
        timed = [r for r in group if r.favour_first is not None]
        out.append({
            "label": label,
            "n": len(group),
            "bias_judged": len(judged),
            "bias_accuracy": (round(sum(1 for r in judged
                                        if r.bias_verdict == CORRECT) / len(judged), 3)
                              if judged else None),
            "trades": len(traded),
            "avg_r": (round(float(np.mean([r.r_multiple for r in traded])), 3)
                      if traded else None),
            "avg_mfe_r": (round(float(np.mean([r.mfe_r for r in traded])), 3)
                          if traded else None),
            "avg_mae_r": (round(float(np.mean([r.mae_r for r in traded])), 3)
                          if traded else None),
            "favour_first_rate": (
                round(sum(1 for r in timed if r.favour_first) / len(timed), 3)
                if timed else None),
            "interpretation": (
                f"{len(group)} signal(s) — too few to read as a result."
                if len(group) < MIN_MEANINGFUL
                else f"{len(group)} signals — worth a second look."),
        })
    return out


def build(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
          lookback: int = PLAN_LOOKBACK_BARS,
          include_rows: bool = False) -> TwoLayerReport:
    """Replay every evaluated signal through the two-layer model."""
    picked = outcome_study.collect(db, symbol, timeframe)
    candles = picked.candles

    report = TwoLayerReport(
        symbol=symbol, timeframe=timeframe,
        selection=picked.report.to_dict(),
        candles=outcome_study.candle_span(candles),
        caveats=CAVEATS)

    if candles.empty or not picked.outcomes:
        return report

    feed = HistoricalFeed(candles)
    stamps = feed._frame["timestamp"]                        # noqa: SLF001
    positions = {stamp.isoformat(): i for i, stamp in enumerate(stamps)}

    rows: list[Replayed] = []
    for outcome in picked.outcomes:
        index = positions.get(pd.Timestamp(outcome.signal_bar_time).isoformat())
        if index is None:
            report.plan_failures += 1
            continue

        # The prefix, and only the prefix. This is what makes the replay
        # legitimate: the plan cannot see a bar the signal could not have.
        window = candles.iloc[max(0, index - lookback + 1) : index + 1]
        try:
            built = plan_builder.build(window, symbol=symbol, timeframe=timeframe)
        except Exception as exc:
            log.debug("plan replay failed at %s: %s", outcome.signal_bar_time, exc)
            report.plan_failures += 1
            continue

        points, atrs = _forward_move(feed, index)
        entry_index = index + 1
        rows.append(Replayed(
            signal_id=outcome.signal_id,
            signal_bar_time=outcome.signal_bar_time,
            action=outcome.action,
            confidence=outcome.confidence,
            bias=built.bias["label"],
            bias_confidence=built.bias["confidence"],
            entry_state=built.entry["state"],
            entry_confidence=built.entry["confidence"],
            entry_style=built.entry.get("style"),
            regime_day=built.entry.get("regime_day"),
            regime_hour=built.entry.get("regime_hour"),
            forward_points=points,
            forward_atr=atrs,
            bias_verdict=_bias_verdict(built.bias["label"], atrs),
            outcome=outcome.outcome,
            r_multiple=outcome.r_multiple,
            mfe_r=outcome.mfe_r,
            mae_r=outcome.mae_r,
            favour_first=_favour_first(feed, entry_index, outcome.action,
                                       outcome.entry, outcome.risk_per_unit),
        ))

    report.replayed = len(rows)
    report.bias_accuracy = _accuracy(rows)
    report.bias_by_regime = _bucket_rows(rows, lambda r: r.regime_hour or "unknown")
    report.entry_states = _bucket_rows(rows, lambda r: r.entry_state)
    report.entry_timing = _bucket_rows(
        rows, lambda r: f"{r.entry_state} / {r.bias}")
    report.engine_agreement = _agreement(rows)
    report.reinterpretation = _reinterpret(rows)
    if include_rows:
        report.rows = [r.to_dict() for r in rows]
    return report


def _accuracy(rows: list[Replayed]) -> dict:
    """Bias accuracy, and the base rate it has to beat to mean anything."""
    judged = [r for r in rows if r.bias_verdict in (CORRECT, WRONG)]
    counts = {name: sum(1 for r in rows if r.bias_verdict == name)
              for name in (CORRECT, WRONG, FLAT, NO_CLAIM)}

    # What a permanently bullish model would have scored on the same bars.
    # Without this the accuracy number is unreadable.
    ups = 0
    for row in rows:
        if row.bias_verdict in (CORRECT, WRONG) and row.forward_atr is not None:
            ups += 1 if row.forward_atr > 0 else 0
    base_rate = round(max(ups, len(judged) - ups) / len(judged), 3) if judged else None

    accuracy = (round(counts[CORRECT] / len(judged), 3) if judged else None)
    return {
        "judged": len(judged),
        "counts": counts,
        "accuracy": accuracy,
        "base_rate": base_rate,
        "edge_over_base_rate": (round(accuracy - base_rate, 3)
                                if accuracy is not None and base_rate is not None
                                else None),
        "horizon_bars": BIAS_HORIZON_BARS,
        "note": ("Accuracy is a directional claim over the next hour, not "
                 "trade success. `base_rate` is what always calling the "
                 "majority direction would have scored on the same bars; "
                 "anything at or below it is no information."),
    }


def _agreement(rows: list[Replayed]) -> dict:
    """How often the new bias points where the old verdict pointed."""
    comparable = [r for r in rows if r.agrees_with_engine is not None]
    agree = [r for r in comparable if r.agrees_with_engine]
    disagree = [r for r in comparable if not r.agrees_with_engine]

    def avg(group):
        traded = [r.r_multiple for r in group if r.r_multiple is not None]
        return round(float(np.mean(traded)), 3) if traded else None

    return {
        "comparable": len(comparable),
        "neutral_bias": len(rows) - len(comparable),
        "agree": len(agree),
        "disagree": len(disagree),
        "avg_r_when_agreeing": avg(agree),
        "avg_r_when_disagreeing": avg(disagree),
        "note": ("The engine's BUY/SELL against the new higher-timeframe "
                 "bias. Disagreement is not an error on either side — they "
                 "answer different questions on different timeframes — but a "
                 "large gap in average R between the two is worth reading."),
    }


def _reinterpret(rows: list[Replayed]) -> dict:
    """The headline: what the two-layer model would have done with these.

    Reported as a measurement, not as a backtest of a gate. No signal was
    filtered — the gate is a later step — and the difference below is a
    property of 167 non-independent observations over thirteen days, which
    is not enough to act on.
    """
    allowed = [r for r in rows if r.entry_state == plan_builder.ENTER_NOW]
    held = [r for r in rows if r.entry_state != plan_builder.ENTER_NOW]

    def stats(group):
        traded = [r for r in group if r.r_multiple is not None]
        if not traded:
            return {"n": len(group), "trades": 0, "avg_r": None,
                    "total_r": None, "win_rate": None, "avg_mfe_r": None}
        rs = [r.r_multiple for r in traded]
        return {
            "n": len(group),
            "trades": len(traded),
            "avg_r": round(float(np.mean(rs)), 3),
            "total_r": round(float(np.sum(rs)), 3),
            "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 3),
            "avg_mfe_r": round(float(np.mean([r.mfe_r for r in traded])), 3),
        }

    return {
        "would_enter_now": stats(allowed),
        "would_wait_or_refuse": stats(held),
        "note": ("What the model would have said, applied to signals it did "
                 "not gate. Suggestive at best: 167 observations over "
                 "thirteen trading days, and consecutive signals inside one "
                 "move are not independent evidence."),
    }
