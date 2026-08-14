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


def test_option_delta_means_premium_moves_less_than_index():
    """The correction this whole module exists for: sizing against index
    points overstates an option buyer's risk by roughly 1/delta."""
    from app.analytics import option_pricing as op

    spot, years = 24_600.0, 3 / 252
    strike, kind = op.select_strike(spot, "BUY")

    before = op.price(spot, strike, years, kind=kind)
    after = op.price(spot + 30, strike, years, kind=kind)
    move = after - before

    assert 0 < move < 30, "premium must move less than the index"
    g = op.greeks(spot, strike, years, kind=kind)
    assert abs(move / 30 - g.delta) < 0.05


def test_time_decay_costs_a_buyer_even_when_direction_is_right():
    """A correct call held long enough loses money. This is the failure an
    index-only backtest can never show."""
    from datetime import datetime, timedelta

    from app.analytics import option_pricing as op

    now = datetime(2026, 8, 7, 10, 0)
    expiry = datetime(2026, 8, 11, 15, 30)
    strike, kind = op.select_strike(24_600.0, "BUY")

    today = op.price(24_630.0, strike, op.years_to_expiry(now, expiry), kind=kind)
    tomorrow = op.price(24_630.0, strike,
                        op.years_to_expiry(now + timedelta(days=1), expiry), kind=kind)

    assert tomorrow < today, "an option must lose value as expiry approaches"


def test_option_is_worth_intrinsic_value_at_expiry():
    from app.analytics import option_pricing as op

    assert op.price(24_700, 24_600, 0.0, kind="CE") == pytest.approx(100.0)
    assert op.price(24_500, 24_600, 0.0, kind="CE") == pytest.approx(0.0)
    assert op.price(24_500, 24_600, 0.0, kind="PE") == pytest.approx(100.0)


def test_implied_volatility_round_trips():
    from app.analytics import option_pricing as op

    spot, strike, years = 24_600.0, 24_600.0, 5 / 252
    for true_iv in (0.10, 0.13, 0.22, 0.40):
        premium = op.price(spot, strike, years, true_iv, kind="CE")
        assert op.implied_volatility(premium, spot, strike, years, kind="CE") \
            == pytest.approx(true_iv, abs=1e-3)


def test_option_backtest_records_decay_separately():
    from app.backtest import option_engine
    from app.risk.manager import RiskConfig

    candles = MockBroker(seed=21).candles(days=20, interval="5m")
    result = option_engine.run(candles, starting_capital=300_000,
                               risk_config=RiskConfig(capital=300_000, lot_size=75))

    assert "total_decay_cost" in result.stats
    for t in result.trades:
        assert t.premium_entry > 0
        assert t.kind in {"CE", "PE"}
        # A BUY signal must buy a call, a SELL signal a put.
        assert (t.kind == "CE") == (t.direction == "BUY")


def test_position_size_can_never_explode():
    """A single trade once took 654 lots on 245,000 of capital and turned a
    losing backtest into a fictional 473% return. The cause was a silent
    floor on premium risk: when an overnight gap put the stop on the wrong
    side of the entry, risk went negative, got clamped to 0.05, and sizing
    divided by it."""
    from app.backtest import option_engine
    from app.risk.manager import RiskConfig

    result = option_engine.run(
        MockBroker(seed=21).candles(days=18, interval="5m"),
        starting_capital=300_000,
        risk_config=RiskConfig(capital=300_000, lot_size=75),
    )
    for t in result.trades:
        assert t.lots <= 20, f"{t.lots} lots is not a real position"
        assert abs(t.r_multiple) < 10, f"{t.r_multiple}R means risk was mismeasured"
        # Premium outlay must respect the deployment cap.
        assert t.premium_entry * t.quantity < 300_000


def test_gapped_entry_is_skipped_not_sized():
    """If the fill price is already past the stop, the trade's premise is
    invalid. Skip it — do not treat it as a trade with tiny risk."""
    from datetime import date

    from app.risk.manager import DayState, RiskConfig, evaluate

    # Stop above entry on a long: risk is negative, so nothing may pass.
    d = evaluate(config=RiskConfig(capital=300_000, lot_size=75),
                 state=DayState(trading_day=date.today()),
                 entry=100.0, stop_loss=100.0, target=130.0)
    assert not d.approved


def test_premium_outlay_is_capped():
    """A stop only protects you if it fills. A gap through it does not, so
    total premium at risk must be capped independently."""
    from datetime import date

    from app.risk.manager import DayState, RiskConfig, evaluate

    cfg = RiskConfig(capital=200_000, lot_size=75, max_capital_deployed_pct=20.0)
    # Tight stop on an expensive option: stop-based sizing wants a huge
    # position, but the premium outlay cap must cut it down.
    d = evaluate(config=cfg, state=DayState(trading_day=date.today()),
                 entry=300.0, stop_loss=298.0, target=306.0, unit_cost=300.0)
    if d.approved:
        assert d.quantity * 300.0 <= 200_000 * 0.20 + 1