"""Tests that pin down the maths. If these break, a signal changed meaning."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import indicators, options, signal_engine, smc, structure  # noqa: E402
from app.brokers.mock import MockBroker  # noqa: E402
from app.risk.manager import DayState, RiskConfig, evaluate  # noqa: E402


@pytest.fixture(scope="module")
def candles():
    return indicators.enrich(MockBroker().candles(days=5))


def test_vwap_resets_each_session(candles):
    key = indicators.session_key(candles)
    first_bars = candles.groupby(key).head(1)
    typical = (first_bars["high"] + first_bars["low"] + first_bars["close"]) / 3
    # On the first bar of a session VWAP equals that bar's typical price.
    assert (first_bars["vwap"] - typical).abs().max() < 0.01


def test_atr_is_positive(candles):
    assert candles["atr14"].dropna().gt(0).all()


def test_swings_are_not_repainted(candles):
    swings = structure.find_swings(candles, lookback=3)
    assert swings, "expected at least one swing in 5 days of data"
    for s in swings:
        window = candles.iloc[s.index - 3 : s.index + 4]
        if s.kind == "high":
            assert s.price == window["high"].max()
        else:
            assert s.price == window["low"].min()


def test_fvg_gaps_are_real_imbalances(candles):
    for gap in smc.find_fair_value_gaps(candles):
        assert gap.top > gap.bottom
        prev_high = candles["high"].iloc[gap.index - 1]
        next_low = candles["low"].iloc[gap.index + 1]
        if gap.direction == "bullish":
            assert next_low > prev_high


def test_max_pain_sits_inside_the_strike_range():
    broker = MockBroker()
    chain = options.validate_chain(broker.option_chain())
    mp = options.max_pain(chain)
    assert chain["strike"].min() <= mp <= chain["strike"].max()


def test_signal_has_a_reason_for_every_check(candles):
    broker = MockBroker()
    sig = signal_engine.generate(candles, chain=broker.option_chain(), india_vix=14.0)
    assert sig.action in {"BUY", "SELL", "HOLD"}
    assert 0 <= sig.confidence <= 1
    assert len(sig.checks) == len(signal_engine.WEIGHTS)
    assert all(c.reason for c in sig.checks)
    if sig.action != "HOLD":
        assert sig.stop_loss != sig.entry
        assert sig.risk_reward >= 2.0


def test_weights_sum_to_one():
    assert abs(sum(signal_engine.WEIGHTS.values()) - 1.0) < 1e-9


def test_risk_blocks_a_third_trade():
    cfg = RiskConfig(capital=100_000, lot_size=75)
    state = DayState(trading_day=pd.Timestamp.today().date(), trades_taken=2)
    d = evaluate(config=cfg, state=state, entry=100, stop_loss=90, target=120)
    assert not d.approved
    assert any("cap" in r for r in d.reasons)


def test_risk_blocks_poor_reward_to_risk():
    cfg = RiskConfig(capital=100_000, lot_size=75)
    state = DayState(trading_day=pd.Timestamp.today().date())
    d = evaluate(config=cfg, state=state, entry=100, stop_loss=90, target=105)
    assert not d.approved
    assert any("Reward:risk" in r for r in d.reasons)


def test_kill_switch_blocks_everything():
    cfg = RiskConfig(capital=1_000_000, lot_size=75, kill_switch=True)
    state = DayState(trading_day=pd.Timestamp.today().date())
    d = evaluate(config=cfg, state=state, entry=100, stop_loss=90, target=130)
    assert not d.approved


def test_constant_volume_is_not_treated_as_information(candles):
    """Yahoo reports zero volume for ^NSEI, so the free broker substitutes a
    constant. A constant must disable the volume check, not read as neutral
    participation — otherwise every signal carries a score based on nothing.
    """
    flat = candles.copy()
    flat["volume"] = 1.0
    assert not indicators.has_real_volume(flat)
    assert indicators.has_real_volume(candles)

    sig = signal_engine.generate(flat)
    volume_check = next(c for c in sig.checks if c.name == "volume")
    assert volume_check.disabled
    assert volume_check.contribution == 0.0


def test_disabled_checks_renormalise_the_weights(candles):
    """With checks switched off the survivors must still be able to reach
    full confidence, or the threshold silently gets stricter."""
    flat = candles.copy()
    flat["volume"] = 1.0

    sig = signal_engine.generate(flat)          # no chain, no volume
    # Volume and the chain must always disable here. Other checks may also
    # disable depending on the data — asserting an exact set made this test
    # break every time a new check learned to switch itself off.
    assert {"volume", "option_chain"} <= set(sig.context["disabled_checks"])

    live_weight = sum(c.weight for c in sig.checks if not c.disabled)
    assert sig.context["weight_scale"] == pytest.approx(1 / live_weight, rel=1e-3)
    assert 0 <= sig.confidence <= 1


def test_scale_always_matches_the_surviving_weight(candles):
    """The invariant that actually matters: whatever is disabled, the live
    checks must together be able to reach full confidence."""
    broker = MockBroker()
    sig = signal_engine.generate(candles, chain=broker.option_chain(), india_vix=13.0)

    live_weight = sum(c.weight for c in sig.checks if not c.disabled)
    assert sig.context["weight_scale"] == pytest.approx(1 / live_weight, rel=1e-3)
    assert sum(c.contribution for c in sig.checks if c.disabled) == 0.0
    if not sig.context["disabled_checks"]:
        assert sig.context["weight_scale"] == pytest.approx(1.0)


def test_stale_structure_break_is_discounted(candles):
    """A CHoCH from five hours ago is not evidence about right now. Live
    output showed a bullish CHoCH at full weight while price sat below the
    level it supposedly broke."""
    from app.analytics.structure import StructureEvent, StructureState

    state = StructureState(trend="bullish")
    state.events.append(StructureEvent(
        index=100, timestamp=candles["timestamp"].iloc[100],
        kind="CHOCH", direction="bullish", broken_level=24500.0, close=24510.0))

    fresh = signal_engine.check_structure(state, current_index=102, price=24510.0)
    aging = signal_engine.check_structure(state, current_index=140, price=24510.0)
    stale = signal_engine.check_structure(state, current_index=200, price=24510.0)

    assert abs(fresh.score) > abs(aging.score) > 0
    assert stale.disabled and stale.contribution == 0.0


def test_a_break_price_traded_back_through_is_downweighted(candles):
    """This is the exact live case: bullish CHoCH broke 24501.35, price then
    24494.29 — below it. The break failed and must not score full strength."""
    from app.analytics.structure import StructureEvent, StructureState

    state = StructureState(trend="bullish")
    state.events.append(StructureEvent(
        index=100, timestamp=candles["timestamp"].iloc[100],
        kind="CHOCH", direction="bullish", broken_level=24501.35, close=24505.0))

    held = signal_engine.check_structure(state, 102, price=24510.0)
    failed = signal_engine.check_structure(state, 102, price=24494.29)

    assert failed.score < held.score
    assert "failed" in failed.reason


def test_distant_fvg_does_not_move_the_signal(candles):
    """A gap 300 points away scored the same as one price was about to trade
    into, quietly pushing every signal around."""
    from app.analytics.smc import FairValueGap

    price, atr = 24494.0, 25.0
    near = FairValueGap(10, candles["timestamp"].iloc[10], "bullish", 24510.0, 24500.0)
    far = FairValueGap(10, candles["timestamp"].iloc[10], "bearish", 24832.0, 24819.0)

    assert signal_engine.check_fvg([near], price, atr).contribution != 0.0

    far_check = signal_engine.check_fvg([far], price, atr)
    assert far_check.disabled
    assert far_check.contribution == 0.0
    assert "too far" in far_check.reason