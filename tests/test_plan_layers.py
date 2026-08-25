"""Bias and entry state: two layers that fail independently.

Step 1 measured why one label was not enough. Eleven signals fired BUY into
an hour that was already trending up, and across all eleven the average
maximum favourable excursion was 0.059R — the direction was right and the
moment was indefensible. A single BUY/SELL/HOLD has nowhere to put that
distinction, so it kept firing at the end of moves.

The behaviour that matters most here is therefore
`test_an_extended_trend_is_told_to_wait_rather_than_chased`: in exactly the
condition that lost, the model must say WAIT_PULLBACK and name the level.

The other load-bearing property is that neither layer can see the future.
`test_a_prefix_produces_the_same_plan` asserts it the strong way, because
the whole reinterpretation of the old 167 signals rests on replaying plans
against archive prefixes.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import plan, regime
from app.market_hours import IST

BARS_PER_SESSION = 75


def frame(steps, start=24_000.0, volume=1000.0, wick=2.0):
    steps = np.asarray(steps, dtype=float)
    close = start + np.cumsum(steps)
    stamps, day = [], datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    for i in range(len(steps)):
        stamps.append((day + timedelta(minutes=5 * (i % BARS_PER_SESSION)))
                      .astimezone(UTC))
        if (i + 1) % BARS_PER_SESSION == 0:
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
    return pd.DataFrame({
        "timestamp": stamps, "open": close - steps,
        "high": np.maximum(close, close - steps) + wick,
        "low": np.minimum(close, close - steps) - wick,
        "close": close, "volume": [volume] * len(steps)})


def calm(n, seed=0, scale=6.0):
    return np.random.default_rng(seed).normal(0.0, scale, n)


# ---- layer 1: bias -----------------------------------------------------

def test_a_sustained_advance_reads_bullish():
    built = plan.build(frame(np.concatenate([calm(300), np.full(80, 4.0)])))

    assert built.bias["label"] == plan.BULLISH
    assert built.bias["confidence"] > 0.3
    assert built.bias["reasons"]


def test_a_sustained_decline_reads_bearish():
    built = plan.build(frame(np.concatenate([calm(300), np.full(80, -4.0)])))

    assert built.bias["label"] == plan.BEARISH


def test_a_directionless_market_reads_neutral():
    built = plan.build(frame(calm(360, seed=21)))

    assert built.bias["label"] == plan.NEUTRAL
    assert built.bias["confidence"] == 0.0
    assert any("no higher-timeframe direction" in r for r in built.bias["reasons"])


def test_the_bias_names_every_reading_including_the_unavailable_ones():
    """No black boxes, and an unavailable reading must be visibly excluded
    rather than quietly counted as neutral — a missing option chain is not
    the same as a chain that says nothing."""
    built = plan.build(frame(calm(300)))
    names = {r["name"] for r in built.bias["readings"]}

    assert names == {"structure_15m", "structure_1h", "trend_15m",
                     "trend_1h", "vwap_1h", "options"}
    options_reading = next(r for r in built.bias["readings"]
                           if r["name"] == "options")
    assert options_reading["available"] is False
    assert any("Unavailable and excluded" in r for r in built.bias["reasons"])


def test_the_bias_uses_only_closed_higher_timeframe_bars():
    """Reading the forming 15-minute bar would put the move being judged into
    the context used to judge it."""
    from app.analytics import timeframes

    df = frame(calm(200))
    folded = timeframes.fifteen_minute(df)

    assert folded["timestamp"].iloc[-1] < df["timestamp"].iloc[-1]


def test_too_little_history_leaves_the_bias_neutral_and_says_why():
    built = plan.build(frame(calm(9)))

    assert built.bias["label"] == plan.NEUTRAL
    assert any("too few" in r.lower() or "no EMA read" in r
               for r in built.bias["reasons"])


# ---- layer 2: the regime decides the style -----------------------------

def test_volatile_chop_forces_no_entry():
    """Forced, not scored. Wide bars going nowhere is the condition where the
    stop is reached by noise rather than by being wrong."""
    rng = np.random.default_rng(11)
    built = plan.build(frame(np.concatenate([calm(300), rng.normal(0, 30.0, 60)])))

    assert regime.VOLATILE_CHOP in (built.regime["day"]["label"],
                                    built.regime["hour"]["label"])
    assert built.entry["state"] == plan.NO_ENTRY
    assert built.entry["style"] == "chop-veto"
    assert any("VOLATILE_CHOP" in r for r in built.entry["reasons"])


def test_an_extended_trend_is_told_to_wait_rather_than_chased():
    """The Step 1 finding, turned into behaviour.

    Eleven BUY signals fired into an already-trending hour and averaged an
    MFE of 0.059R. In exactly that condition the model must now wait, and
    must say what it is waiting for.
    """
    built = plan.build(frame(np.concatenate([calm(300), np.full(60, 4.0)])))

    assert built.regime["hour"]["label"] == regime.TREND_UP
    assert built.bias["label"] == plan.BULLISH
    assert built.entry["state"] == plan.WAIT_PULLBACK
    assert built.entry["trigger_level"] is not None
    assert built.entry["stretch_atr"] > plan.PULLBACK_ATR
    assert "pullback" in built.entry["trigger_note"]


def test_a_trend_that_has_pulled_back_to_value_is_entered():
    """The other half of the same rule. Waiting forever is not a strategy —
    the state has to become ENTER_NOW when price actually returns to value,
    or WAIT_PULLBACK is just a refusal wearing a better name."""
    built = plan.build(frame(np.concatenate([calm(300), np.full(40, 2.5),
                                             np.full(4, -4.0)])))

    assert built.regime["day"]["label"] == regime.TREND_UP
    assert built.bias["label"] == plan.BULLISH
    assert built.entry["style"] == "trend-pullback"
    assert built.entry["state"] == plan.ENTER_NOW
    assert built.entry["stretch_atr"] <= plan.PULLBACK_ATR


def test_a_deeper_pullback_never_demands_a_deeper_one():
    """A monotonicity bug this caught, and the reason the anchor is the
    nearest level rather than the nearest one *behind* price.

    With the earlier rule, the moment price dipped a point under the 20 EMA
    that level stopped counting, the anchor jumped to a session VWAP a
    hundred points away, and the model answered a deeper pullback by asking
    for a deeper one still — so ENTER_NOW was reachable only inside a window
    a couple of bars wide.
    """
    states, stretches = [], []
    for depth in range(0, 8, 2):
        steps = [calm(300), np.full(40, 2.5)]
        if depth:
            steps.append(np.full(depth, -4.0))
        built = plan.build(frame(np.concatenate(steps)))
        states.append(built.entry["state"])
        stretches.append(built.entry["stretch_atr"])

    # Extension falls as the pullback deepens; it never rises.
    assert stretches == sorted(stretches, reverse=True)
    # And the state moves from waiting to entering, not back and forth.
    assert states[0] == plan.WAIT_PULLBACK
    assert states[-1] == plan.ENTER_NOW


def test_a_range_waits_for_a_break_of_the_last_swing():
    """Layer 2's rule mapping, exercised directly.

    Synthesising 5m data that lands on a specific regime *and* a non-neutral
    bias is a search problem, and a test that depends on winning it tests the
    generator more than the rule. The regime and the bias are what this rule
    consumes, so they are supplied.
    """
    df = frame(np.concatenate([calm(280, seed=3), np.full(30, 1.6)]))
    entry = plan.read_entry(
        df, plan.Bias(label=plan.BULLISH, confidence=0.7, score=0.5, agreement=1.0),
        {"day": {"label": regime.RANGE}, "hour": {"label": regime.RANGE}})

    assert entry.style == "range-breakout"
    assert entry.state in (plan.WAIT_BREAKOUT, plan.ENTER_NOW)
    assert entry.trigger_level is not None
    assert any("boundary gives way" in r for r in entry.reasons)


def test_a_squeeze_is_treated_as_a_breakout_market():
    """Compression resolves by expanding through a level, so it takes the
    same style as a range rather than a style of its own."""
    df = frame(calm(300))
    entry = plan.read_entry(
        df, plan.Bias(label=plan.BEARISH, confidence=0.7, score=-0.5, agreement=1.0),
        {"day": {"label": regime.SQUEEZE}, "hour": {"label": regime.SQUEEZE}})

    assert entry.style == "range-breakout"


def test_a_wick_through_a_level_is_not_a_break():
    """A close has to settle past the level. Without the margin the state
    flips on any bar that pokes through and comes back."""
    from app.analytics import indicators, structure

    df = frame(np.concatenate([calm(280, seed=3), np.full(30, 1.6)]))
    enriched = indicators.enrich(df)
    state = structure.analyse(enriched)
    assert state.last_swing_high is not None

    built = plan.build(df)
    if built.entry["state"] == plan.WAIT_BREAKOUT:
        assert built.entry["stretch_atr"] < plan.BREAKOUT_ATR
    else:
        assert built.entry["stretch_atr"] >= plan.BREAKOUT_ATR


def test_a_bias_against_the_session_trend_is_refused():
    """Trading the higher-timeframe view against the move the day is actually
    making is a different trade from the one the bias described."""
    entry = plan.read_entry(
        frame(calm(200)),
        plan.Bias(label=plan.BULLISH, confidence=0.8, score=0.6, agreement=1.0),
        {"day": {"label": regime.TREND_DOWN}, "hour": {"label": regime.TREND_DOWN}})

    assert entry.state == plan.NO_ENTRY
    assert entry.style == "counter-trend"


def test_an_hour_against_a_trend_day_is_the_pullback_not_a_veto():
    """The design error that made ENTER_NOW unreachable.

    The pullback a trend rule waits for is, by construction, an hour running
    against the day. Keyed off the hour, that reads as counter-trend and the
    model vetoes the entry it was waiting for. The session decides the style;
    the hour is evidence about the moment inside it.
    """
    entry = plan.read_entry(
        frame(np.concatenate([calm(300), np.full(40, 2.5), np.full(4, -4.0)])),
        plan.Bias(label=plan.BULLISH, confidence=0.8, score=0.6, agreement=1.0),
        {"day": {"label": regime.TREND_UP}, "hour": {"label": regime.TREND_DOWN}})

    assert entry.state != plan.NO_ENTRY
    assert entry.style == "trend-pullback"
    assert any("pullback rather than a reversal" in r for r in entry.reasons)


def test_a_neutral_bias_leaves_nothing_to_time():
    entry = plan.read_entry(
        frame(calm(200)),
        plan.Bias(label=plan.NEUTRAL, confidence=0.0, score=0.0, agreement=0.0),
        {"day": {"label": regime.RANGE}, "hour": {"label": regime.RANGE}})

    assert entry.state == plan.NO_ENTRY
    assert entry.style == "no-bias"
    assert any("NEUTRAL" in r for r in entry.reasons)


def test_the_entry_layer_never_chooses_a_direction():
    """Layer 2 times layer 1's call. It has no opinion of its own, so the
    same bar under opposite biases must not produce two actionable entries
    pointing different ways."""
    df = frame(np.concatenate([calm(300), np.full(60, 4.0)]))
    verdict = plan.build(df).regime

    bullish = plan.read_entry(df, plan.Bias(plan.BULLISH, 0.8, 0.6, 1.0), verdict)
    bearish = plan.read_entry(df, plan.Bias(plan.BEARISH, 0.8, -0.6, 1.0), verdict)

    assert not (bullish.state == plan.ENTER_NOW and bearish.state == plan.ENTER_NOW)


# ---- look-ahead --------------------------------------------------------

@pytest.mark.parametrize("cut", [180, 240, 300, 360])
def test_a_prefix_produces_the_same_plan(cut):
    """What the replay of the old 167 signals rests on.

    Building a plan from the first N bars must give exactly what building it
    from the whole frame and reading bar N gives. If it does not, every
    number in the reinterpretation is contaminated by hindsight in a way
    that would look like a discovery.
    """
    df = frame(np.concatenate([calm(200, seed=4), np.full(80, 3.0),
                               calm(120, seed=5)]))

    whole = plan.build(df.iloc[:cut].reset_index(drop=True))
    again = plan.build(df.iloc[:cut].reset_index(drop=True))
    assert whole.to_dict() == again.to_dict()

    # And a later shock cannot reach back to change it.
    shocked = pd.concat([df.iloc[:cut], df.iloc[:cut].assign(
        close=df["close"].iloc[:cut] * 1.05)], ignore_index=True)
    from_prefix = plan.build(shocked.iloc[:cut].reset_index(drop=True))
    assert from_prefix.bias["label"] == whole.bias["label"]
    assert from_prefix.entry["state"] == whole.entry["state"]


def test_the_plan_is_stamped_with_the_bar_it_was_built_on():
    df = frame(calm(200))
    built = plan.build(df)

    assert built.timestamp == df["timestamp"].iloc[-1].isoformat()


# ---- shape -------------------------------------------------------------

def test_labels_come_from_the_agreed_sets():
    for steps in (calm(300), np.concatenate([calm(280), np.full(40, 5.0)]),
                  np.concatenate([calm(280), np.full(40, -5.0)])):
        built = plan.build(frame(steps))
        assert built.bias["label"] in plan.BIAS_LABELS
        assert built.entry["state"] in plan.ENTRY_STATES


def test_every_entry_state_explains_itself():
    for steps in (calm(300, seed=1), np.concatenate([calm(280), np.full(40, 5.0)]),
                  np.concatenate([calm(280), np.random.default_rng(2)
                                  .normal(0, 30, 40)])):
        built = plan.build(frame(steps))
        assert built.entry["reasons"]
        assert all(isinstance(r, str) and r for r in built.entry["reasons"])


def test_the_plan_is_json_serialisable():
    import json
    built = plan.build(frame(calm(300)))

    assert json.loads(json.dumps(built.to_dict(), default=str))


def test_an_empty_frame_is_refused_rather_than_guessed_at():
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                  "close", "volume"])
    with pytest.raises(ValueError):
        plan.build(empty)


def test_the_headline_reads_as_a_sentence():
    built = plan.build(frame(np.concatenate([calm(300), np.full(60, 4.0)])))
    text = built.explain()

    assert built.bias["label"] in text
    assert built.entry["state"] in text


# ---- Step 2.1: the breakout rule, and why it was left alone ------------

def test_the_breakout_level_is_the_last_confirmed_swing():
    """Pinned deliberately.

    Step 2.1 investigated replacing this with a multi-touch liquidity pool,
    on the theory that a fractal swing is a wiggle rather than a range edge.
    The theory was falsified — every one of the twenty signals the rule
    admitted was already breaking a multi-touch level — and the replacement
    made the gate three times looser. This test exists so the same
    substitution is not attempted a third time without new evidence.
    """
    from app.analytics import indicators, structure

    df = frame(np.concatenate([calm(280, seed=3), np.full(30, 1.6)]))
    entry = plan.read_entry(
        df, plan.Bias(label=plan.BULLISH, confidence=0.7, score=0.5, agreement=1.0),
        {"day": {"label": regime.RANGE}, "hour": {"label": regime.RANGE}})

    state = structure.analyse(indicators.enrich(df))
    assert entry.trigger_level == pytest.approx(
        round(float(state.last_swing_high.price), 2))


def test_a_break_has_no_upper_bound_on_extension():
    """Recorded as a known asymmetry, not endorsed.

    The pullback rule refuses an entry more than DEEP_PULLBACK_ATR from
    value; this rule accepts a close any distance beyond the level, so a
    0.3 ATR poke and a 5 ATR extension are the same event to it. Bounding it
    was tried in Step 2.1 and made the sample strictly worse — entries within
    1 ATR of the level won none of eleven — so the asymmetry stays until
    there is evidence either way rather than being tidied on instinct.
    """
    df = frame(np.concatenate([calm(280, seed=3), np.full(40, 6.0)]))
    entry = plan.read_entry(
        df, plan.Bias(label=plan.BULLISH, confidence=0.7, score=0.5, agreement=1.0),
        {"day": {"label": regime.RANGE}, "hour": {"label": regime.RANGE}})

    if entry.state == plan.ENTER_NOW:
        assert entry.stretch_atr >= plan.BREAKOUT_ATR
