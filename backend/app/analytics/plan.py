"""Two layers instead of one verdict: what the market is doing, and whether
this is the moment to act on it.

The single BUY/SELL/HOLD output conflates two questions that fail
independently. Step 1 measured the cost of that. Of the 167 evaluated
signals, the eleven that fired as BUY while the hour was already trending up
had an average maximum favourable excursion of **0.059R** — across all
eleven, the best the trade ever looked was six percent of the risk taken.
The direction was right and the moment was not. A single label has nowhere
to put that distinction, so it kept firing at the end of moves.

So:

  Layer 1, BIAS — BULLISH / BEARISH / NEUTRAL, from the 15-minute and
  hourly views. Slow, and about direction only. It says nothing about
  price levels or timing.

  Layer 2, ENTRY STATE — ENTER_NOW / WAIT_PULLBACK / WAIT_BREAKOUT /
  NO_ENTRY, from the 5-minute view and the regime. Fast, and about the
  moment only. It never chooses a direction; it takes the bias's.

The regime decides which entry style is even available. Trends are entered
on pullbacks, ranges on confirmed breaks, and volatile chop is not entered at
all. That mapping is the whole point: it is what stops the desk buying an
extended move because the evidence finally agreed.

Nothing here changes the signal engine, its weights, its thresholds, or the
risk manager. It reads the same analytics they read and answers a different
question alongside them. It also invents no indicator: structure, EMA
stacking, VWAP position, ATR and the option chain summary are all existing
readings, reached through the existing functions so that a change to any of
them moves both layers together.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from . import indicators, options, regime, signal_engine, structure, timeframes

BULLISH, BEARISH, NEUTRAL = "BULLISH", "BEARISH", "NEUTRAL"
BIAS_LABELS = (BULLISH, BEARISH, NEUTRAL)

ENTER_NOW = "ENTER_NOW"
WAIT_PULLBACK = "WAIT_PULLBACK"
WAIT_BREAKOUT = "WAIT_BREAKOUT"
NO_ENTRY = "NO_ENTRY"
ENTRY_STATES = (ENTER_NOW, WAIT_PULLBACK, WAIT_BREAKOUT, NO_ENTRY)

# Bars needed before a higher-timeframe view means anything. Structure needs
# swings, and a swing needs bars either side of it.
MIN_HTF_BARS = 12

# How far the average reading has to lean before the bias stops being
# NEUTRAL. Six readings voting -1/0/+1: this is roughly "two clear votes
# with nothing against", which is a deliberately low bar because the bias is
# not permission to trade — the entry layer still has to agree.
BIAS_THRESHOLD = 0.25

# How close to VWAP or the 20 EMA price has to come back before a pullback
# counts as finished, in ATRs. Above this the desk waits.
PULLBACK_ATR = 0.75

# How far past a swing level a close has to settle before a break is
# confirmed rather than a wick. Also in ATRs, so it scales with the day.
BREAKOUT_ATR = 0.25

# Beyond this the pullback has gone far enough to question the trend itself.
DEEP_PULLBACK_ATR = 2.0


def _direction(word: str | None) -> float:
    return {"bullish": 1.0, "bearish": -1.0}.get(word or "", 0.0)


# --------------------------------------------------------------------------
# layer 1 — bias
# --------------------------------------------------------------------------

@dataclass
class Reading:
    """One higher-timeframe opinion, and where it came from."""
    name: str
    score: float                 # -1 bearish .. +1 bullish
    reason: str
    available: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Bias:
    label: str
    confidence: float
    score: float                 # the raw mean, signed
    agreement: float             # share of live readings on the winning side
    reasons: list[str] = field(default_factory=list)
    readings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _structure_reading(frame: pd.DataFrame, label: str) -> Reading:
    """The platform's own structure read, on a higher-timeframe frame.

    `structure.analyse` unchanged — a higher-timeframe frame is just a candle
    frame, and reimplementing swing detection for 15-minute bars would be a
    second definition of a break of structure.
    """
    if len(frame) < MIN_HTF_BARS:
        return Reading(f"structure_{label}", 0.0,
                       f"Only {len(frame)} closed {label} bars — too few to "
                       "read structure.", available=False)
    state = structure.analyse(frame)
    score = _direction(state.trend)
    return Reading(f"structure_{label}", score,
                   f"{label} structure is {state.trend}.")


def _trend_reading(frame: pd.DataFrame, label: str) -> Reading:
    """EMA stacking, through the signal engine's own check.

    Only `.score` and `.reason` are taken. The weight travels with the Check
    and is deliberately discarded: those weights are the 5-minute engine's,
    tuned for a different question, and borrowing them here would silently
    couple the bias to a number nobody set for it.
    """
    if len(frame) < MIN_HTF_BARS:
        return Reading(f"trend_{label}", 0.0,
                       f"Only {len(frame)} closed {label} bars — no EMA read.",
                       available=False)
    enriched = indicators.enrich(frame)
    check = signal_engine.check_trend(enriched.iloc[-1])
    # A check that could not run is left out of the bias, not counted as a
    # zero. An hourly frame built from the declared 300 five-minute bars
    # holds about 25 bars — short of the 50 an EMA50 needs — and its EMA50
    # used to be a number seeded from the first close and read as a trend.
    return Reading(f"trend_{label}", check.score, f"{label}: {check.reason}",
                   available=not check.disabled)


def _vwap_reading(frame: pd.DataFrame, label: str) -> Reading:
    """Where the higher-timeframe close sits against session VWAP."""
    if len(frame) < 2:
        return Reading(f"vwap_{label}", 0.0,
                       f"No closed {label} bars yet.", available=False)
    enriched = indicators.enrich(frame)
    row = enriched.iloc[-1]
    if pd.isna(row["vwap"]):
        return Reading(f"vwap_{label}", 0.0, "VWAP not available yet.",
                       available=False)
    check = signal_engine.check_vwap(row)
    return Reading(f"vwap_{label}", check.score, f"{label}: {check.reason}")


def _option_reading(summary: options.ChainSummary | None) -> Reading:
    if summary is None:
        return Reading("options", 0.0,
                       "No option chain available, so positioning is not part "
                       "of this bias.", available=False)
    if not summary.oi_available:
        return Reading("options", 0.0,
                       "Option-chain open interest is unavailable, so "
                       "positioning is not part of this bias.", available=False)
    pcr = (f"PCR {summary.pcr_oi:.2f}" if summary.pcr_oi is not None
           else "PCR unavailable")
    pain = (f"max pain {summary.max_pain:.0f}" if summary.max_pain is not None
            else "max pain unavailable")
    return Reading("options", _direction(summary.bias),
                   f"Chain reads {summary.bias} ({pcr}, {pain}).")


def read_bias(candles: pd.DataFrame,
              chain_summary: options.ChainSummary | None = None) -> Bias:
    """The higher-timeframe directional view.

    Every reading counts once. Deliberately unweighted: there is no outcome
    evidence yet that would justify one weighting over another, and a set of
    numbers chosen because they felt right would be indistinguishable from a
    fitted set once it was in the file. Calibration is a later step with data
    behind it; until then, equal votes are the honest default.
    """
    fifteen = timeframes.fifteen_minute(candles)
    hour = timeframes.hourly(candles)

    readings = [
        _structure_reading(fifteen, "15m"),
        _structure_reading(hour, "1h"),
        _trend_reading(fifteen, "15m"),
        _trend_reading(hour, "1h"),
        _vwap_reading(hour, "1h"),
        _option_reading(chain_summary),
    ]

    live = [r for r in readings if r.available]
    reasons = [r.reason for r in readings]

    if not live:
        return Bias(label=NEUTRAL, confidence=0.0, score=0.0, agreement=0.0,
                    reasons=[*reasons,
                             "No higher-timeframe reading was available, so "
                             "there is no bias — which is not the same as a "
                             "neutral market."],
                    readings=[r.to_dict() for r in readings])

    score = sum(r.score for r in live) / len(live)
    label = (BULLISH if score >= BIAS_THRESHOLD
             else BEARISH if score <= -BIAS_THRESHOLD else NEUTRAL)

    leaning = [r for r in live if r.score != 0]
    if label == NEUTRAL or not leaning:
        agreement = 0.0
    else:
        want = 1.0 if label == BULLISH else -1.0
        agreement = sum(1 for r in leaning
                        if r.score * want > 0) / len(leaning)

    confidence = round(min(1.0, abs(score)) * (0.5 + 0.5 * agreement), 3)
    if label == NEUTRAL:
        confidence = 0.0
        reasons.append(
            f"Readings average {score:+.2f}, inside the ±{BIAS_THRESHOLD} "
            "band — no higher-timeframe direction worth expressing.")
    else:
        reasons.append(
            f"Readings average {score:+.2f} with {agreement:.0%} of the "
            f"leaning ones agreeing — {label}.")

    if len(live) < len(readings):
        missing = [r.name for r in readings if not r.available]
        reasons.append(f"Unavailable and excluded rather than counted as "
                       f"neutral: {', '.join(missing)}.")

    return Bias(label=label, confidence=confidence, score=round(score, 3),
                agreement=round(agreement, 3), reasons=reasons,
                readings=[r.to_dict() for r in readings])


# --------------------------------------------------------------------------
# layer 2 — entry state
# --------------------------------------------------------------------------

@dataclass
class Entry:
    state: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    style: str | None = None            # which regime rule applied
    trigger_level: float | None = None  # the price being waited for
    trigger_note: str | None = None
    stretch_atr: float | None = None    # how extended, in ATRs
    regime_day: str | None = None
    regime_hour: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _anchors(row: pd.Series) -> tuple[float | None, float | None, float | None]:
    """Price, session VWAP and the 20 EMA — the two levels a pullback aims at."""
    def clean(name):
        value = row.get(name)
        return None if value is None or pd.isna(value) else float(value)
    return clean("close"), clean("vwap"), clean("ema20")


def read_entry(candles: pd.DataFrame, bias: Bias, verdict: dict | None) -> Entry:
    """Whether this is the moment, given the bias and the regime.

    Never chooses a direction. If the bias is NEUTRAL there is nothing to
    time, and the answer is NO_ENTRY rather than a guess.
    """
    day = (verdict or {}).get("day", {}).get("label")
    hour = (verdict or {}).get("hour", {}).get("label")
    base = {"regime_day": day, "regime_hour": hour}

    enriched = indicators.enrich(candles)
    row = enriched.iloc[-1]
    price, vwap, ema20 = _anchors(row)
    atr = row.get("atr14")
    atr = None if atr is None or pd.isna(atr) or atr <= 0 else float(atr)

    # --- the vetoes, in order ------------------------------------------
    if bias.label == NEUTRAL:
        return Entry(state=NO_ENTRY, confidence=1.0, style="no-bias", **base,
                     reasons=["The higher-timeframe bias is NEUTRAL — there is "
                              "no directional thesis for an entry to time."])

    if regime.VOLATILE_CHOP in (day, hour):
        # Forced, not scored. Wide bars going nowhere is the condition in
        # which a stop is hit by noise rather than by being wrong, and no
        # amount of entry quality changes that.
        where = "session" if day == regime.VOLATILE_CHOP else "hour"
        return Entry(state=NO_ENTRY, confidence=1.0, style="chop-veto", **base,
                     reasons=[f"The {where} regime is VOLATILE_CHOP. Entries are "
                              "refused outright in this condition — the stop is "
                              "reached by noise rather than by being wrong."])

    if atr is None or price is None:
        return Entry(state=NO_ENTRY, confidence=0.0, style="no-data", **base,
                     reasons=["ATR is not available yet, so neither extension "
                              "nor a breakout margin can be measured."])

    want = 1.0 if bias.label == BULLISH else -1.0

    # Style is a property of the *session*, not of the last hour. Keying it
    # off the hour looks right and is self-defeating: the pullback the trend
    # rule exists to wait for is, by construction, an hour running against
    # the day. Read at hour level that is a counter-trend condition, so the
    # model vetoes the entry it was waiting for and ENTER_NOW becomes
    # unreachable. The first version of this file did exactly that, and
    # `test_a_trend_that_has_pulled_back_to_value_is_entered` caught it.
    #
    # So the day decides which style applies and whether the bias is fighting
    # the dominant move; the hour is evidence about the moment inside it.
    day_trend = {regime.TREND_UP: 1.0, regime.TREND_DOWN: -1.0}.get(day)
    hour_trend = {regime.TREND_UP: 1.0, regime.TREND_DOWN: -1.0}.get(hour)

    if day_trend is not None and day_trend * want < 0:
        return Entry(state=NO_ENTRY, confidence=1.0, style="counter-trend",
                     **base,
                     reasons=[f"The session regime is {day} while the "
                              f"higher-timeframe bias is {bias.label}. Taking "
                              "the bias here means trading against the move "
                              "the day is actually making."])

    if day_trend is not None:
        return _pullback(row, price, vwap, ema20, atr, want, bias, day,
                         hour_trend, base)
    return _breakout(enriched, price, atr, want, bias, day, base)


def _pullback(row, price, vwap, ema20, atr, want, bias, day, hour_trend,
              base) -> Entry:
    """Trend regime, aligned bias. Wait for value unless price is already at it.

    This is the rule Step 1 argued for. Eleven BUY signals fired into an
    already-trending hour and their average best moment was 0.059R; they were
    entries at the end of a move, taken because the evidence had finally piled
    up. Requiring price to come back toward VWAP or the 20 EMA first is what
    converts "the trend is up" from a reason to buy now into a reason to buy
    a dip.
    """
    levels = [(name, value) for name, value in
              (("VWAP", vwap), ("20 EMA", ema20)) if value is not None]
    if not levels:
        return Entry(state=NO_ENTRY, confidence=0.0, style="trend-pullback",
                     **base,
                     reasons=["Neither VWAP nor the 20 EMA is available, so "
                              "there is no level for a pullback to reach."])

    # The nearest of the two levels, in plain distance, and how far past it
    # price sits in the bias's direction. Positive means still extended;
    # zero or negative means price has reached value or traded through it.
    #
    # An earlier version filtered to levels *behind* price — the highest
    # support below a long — which reads as more precise and behaves worse.
    # The moment price dipped a point under the 20 EMA that level stopped
    # counting, the anchor jumped to a session VWAP a hundred points away,
    # and the model responded to a deeper pullback by demanding a deeper one
    # still. Nearest-level is monotone: more pullback never means more
    # waiting.
    name, anchor = min(levels, key=lambda lv: abs(price - lv[1]))
    stretch = (price - anchor) * want / atr

    reasons = [f"Session regime is {day} and the bias agrees, so this is a "
               "pullback market: entries are taken at value, not at extension.",
               f"Price is {stretch:+.2f} ATR from {name} ({anchor:.2f})."]
    if hour_trend is not None and hour_trend * want < 0:
        # Not a veto. In a trend day this is the pullback itself — the hour
        # running against the session is what "wait for a pullback" means
        # when it is actually happening.
        reasons.append("The last hour is running against the session, which "
                       "in a trend day is the pullback rather than a reversal.")

    if stretch > DEEP_PULLBACK_ATR:
        confidence = round(regime.ramp(stretch, PULLBACK_ATR,
                                       DEEP_PULLBACK_ATR * 1.5), 3)
        reasons.append(
            f"More than {DEEP_PULLBACK_ATR} ATR past it — extended enough that "
            "chasing here is the pattern the evaluation found losing.")
        return Entry(state=WAIT_PULLBACK, confidence=confidence,
                     style="trend-pullback", trigger_level=round(anchor, 2),
                     trigger_note=f"wait for a pullback toward {name}",
                     stretch_atr=round(stretch, 2), reasons=reasons, **base)

    if stretch > PULLBACK_ATR:
        return Entry(state=WAIT_PULLBACK,
                     confidence=round(regime.ramp(stretch, PULLBACK_ATR,
                                                  DEEP_PULLBACK_ATR), 3),
                     style="trend-pullback", trigger_level=round(anchor, 2),
                     trigger_note=f"wait for a pullback toward {name}",
                     stretch_atr=round(stretch, 2), reasons=reasons, **base)

    # Close enough to value to act. Confidence rises as price sits nearer the
    # anchor, and the bias's own confidence caps it — timing a direction the
    # higher timeframe is unsure of is not a strong entry.
    quality = regime.ramp_down(abs(stretch), PULLBACK_ATR, 0.0)
    reasons.append("Price has come back to value — this is the entry the "
                   "pullback rule exists to wait for.")
    return Entry(state=ENTER_NOW,
                 confidence=round(min(quality, bias.confidence + 0.25), 3),
                 style="trend-pullback", trigger_level=round(anchor, 2),
                 trigger_note=f"at value against {name}",
                 stretch_atr=round(stretch, 2), reasons=reasons, **base)


def _breakout(enriched, price, atr, want, bias, day, base) -> Entry:
    """Range or squeeze. Nothing to trade until a boundary actually gives way.

    The boundary is the last confirmed swing — the platform's existing
    definition of a level, rather than a rolling high invented here. A close
    has to settle past it by a margin in ATRs, because a wick through a level
    and a close through it are different events and only the second one is a
    break.

    Left as it was after a Step 2.1 investigation that failed to justify
    changing it, which is worth recording so the same ground is not covered
    twice:

      The Step 2 report called this rule anti-selective, on the grounds that
      the twenty signals it admitted averaged -0.71R against the range
      baseline of -0.53R. That gap is 0.68 standard errors. A permutation
      test puts p at 0.27 — better than one run in four of a rule that
      selects at random would look at least this bad. The finding was noise
      and should not have been reported as a lead.

      The diagnosis was wrong too, and testably so. The suspicion was that
      `last_swing_high` is a wiggle rather than a range edge, so the rule was
      confirming breaks of nothing. Requiring the level to carry two or more
      touches — `smc.find_liquidity_pools` — changed the admitted set not at
      all: every one of the twenty was already breaking a multi-touch level.

      Bounding the margin above, so a break is distinguished from a chase,
      makes the sample strictly worse: entries within 1 ATR of the level won
      none of eleven. The only variant that improves anything requires the
      break to have held five bars, which keeps seven signals of a hundred
      and twenty and owes +5.26R of its +0.37R to three trades. Removing any
      one of them turns it negative. That is a threshold fitted to three
      observations, not a rule.

    Twenty observations over thirteen days cannot separate these hypotheses,
    and picking the one that scores best on them is how a system acquires a
    rule that works only on the data that produced it.
    """
    state = structure.analyse(enriched)
    swing = state.last_swing_high if want > 0 else state.last_swing_low
    label = "swing high" if want > 0 else "swing low"

    reasons = [f"Session regime is {day}, so this is a breakout market: the "
               "bias is not actionable until a boundary gives way."]

    if swing is None:
        reasons.append("No confirmed swing to use as a boundary yet.")
        return Entry(state=NO_ENTRY, confidence=0.0, style="range-breakout",
                     reasons=reasons, **base)

    level = float(swing.price)
    margin = (price - level) * want / atr
    reasons.append(f"Price is {margin:+.2f} ATR beyond the last {label} "
                   f"({level:.2f}); a break needs {BREAKOUT_ATR} ATR of close "
                   "past it, not a wick through it.")

    if not indicators.has_real_volume(enriched):
        reasons.append("Volume is synthetic on this feed, so participation "
                       "cannot confirm the break and the margin has to carry "
                       "it alone.")
    else:
        rvol = enriched.iloc[-1].get("rvol")
        if rvol is not None and not pd.isna(rvol):
            reasons.append(f"Relative volume {float(rvol):.2f}x on the break bar.")

    if margin >= BREAKOUT_ATR:
        reasons.append("The level has given way on a close.")
        return Entry(state=ENTER_NOW,
                     confidence=round(min(regime.ramp(margin, BREAKOUT_ATR,
                                                      BREAKOUT_ATR * 4),
                                          bias.confidence + 0.25), 3),
                     style="range-breakout", trigger_level=round(level, 2),
                     trigger_note=f"broke the {label}",
                     stretch_atr=round(margin, 2), reasons=reasons, **base)

    return Entry(state=WAIT_BREAKOUT,
                 confidence=round(regime.ramp_down(margin, BREAKOUT_ATR, -2.0), 3),
                 style="range-breakout", trigger_level=round(level, 2),
                 trigger_note=f"wait for a close beyond the {label}",
                 stretch_atr=round(margin, 2), reasons=reasons, **base)


# --------------------------------------------------------------------------
# both layers
# --------------------------------------------------------------------------

@dataclass
class Plan:
    symbol: str
    timeframe: str
    timestamp: str
    bias: dict
    entry: dict
    regime: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def explain(self) -> str:
        head = f"{self.bias['label']} — {self.entry['state']}"
        note = self.entry.get("trigger_note")
        level = self.entry.get("trigger_level")
        if note and level is not None:
            head += f" ({note}, {level:.2f})"
        elif note:
            head += f" ({note})"
        return head


def build(candles: pd.DataFrame, symbol: str = "NIFTY", timeframe: str = "5m",
          chain: pd.DataFrame | None = None,
          chain_summary: options.ChainSummary | None = None) -> Plan:
    """Both layers for the most recent bar in `candles`.

    Causal by construction: every input is derived from this frame, and the
    higher-timeframe folds hold back the bar still forming. Handing it a
    prefix of the archive therefore reproduces exactly what the desk would
    have said at that bar, which is what makes replaying the old signals
    against it legitimate.

    The bar still forming is dropped here rather than trusted to the
    caller. The folds held back an incomplete *fold*, but the base frame's
    own last bar went straight into the bias and entry readings: injecting
    one changed 17 of 25 sampled plans while leaving the signal untouched.
    `decision_time` on the frame says when the plan is being made; without
    it the plan is being made now.
    """
    decision_time = candles.attrs.get("decision_time", pd.Timestamp.now(tz="UTC"))
    candles = indicators.drop_unclosed(candles, timeframe, as_of=decision_time)
    frame = indicators.validate(candles)
    if frame.empty:
        raise ValueError("cannot build a plan from an empty candle frame")

    price = float(frame["close"].iloc[-1])
    if chain_summary is None and chain is not None:
        chain_summary = options.summarise(chain, price)

    bias = read_bias(frame, chain_summary)
    # Already filtered above; the clock is passed anyway so the regime
    # reading is bound to the same decision instant rather than to now.
    verdict = regime.classify_latest(frame, as_of=decision_time, timeframe=timeframe)
    entry = read_entry(frame, bias, verdict)

    return Plan(symbol=symbol, timeframe=timeframe,
                timestamp=frame["timestamp"].iloc[-1].isoformat(),
                bias=bias.to_dict(), entry=entry.to_dict(), regime=verdict)
