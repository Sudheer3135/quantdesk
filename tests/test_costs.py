"""Tests for transaction costs and slippage.

These deliberately assert *properties* rather than rupee totals. The rates
change by circular, and a test pinned to "₹412.83" would fail the day
somebody correctly updates STT — training everyone to edit the expected
number until it goes green, which is how a cost model quietly stops
describing reality.

What must hold regardless of the rates: the breakdown sums, costs scale
with size, nothing is negative, and the shape is right.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.backtest.costs import (
    CostModel,
    FlatCostModel,
    SlippageModel,
    buy_fill,
    describe,
    sell_fill,
)

# ---- the breakdown ----------------------------------------------------

def test_the_parts_add_up_to_the_total():
    """If this drifts, every itemised report lies about where money went."""
    breakdown = CostModel().round_trip(buy_price=120.0, sell_price=150.0, quantity=75)
    parts = (breakdown.brokerage + breakdown.stt + breakdown.exchange
             + breakdown.sebi + breakdown.ipft + breakdown.stamp_duty + breakdown.gst)
    assert breakdown.total == pytest.approx(parts)
    assert breakdown.to_dict()["total"] == pytest.approx(round(breakdown.total, 2))


def test_stt_is_charged_on_the_sell_leg_only():
    """The single largest charge for a retail option buyer, and the one most
    often modelled wrongly as symmetric."""
    model = CostModel()
    cheap_exit = model.round_trip(buy_price=200.0, sell_price=10.0, quantity=75)
    rich_exit = model.round_trip(buy_price=10.0, sell_price=200.0, quantity=75)
    assert rich_exit.stt > cheap_exit.stt

    expired_worthless = model.round_trip(buy_price=200.0, sell_price=0.0, quantity=75)
    assert expired_worthless.stt == 0.0


def test_stamp_duty_is_charged_on_the_buy_leg_only():
    model = CostModel()
    a = model.round_trip(buy_price=200.0, sell_price=10.0, quantity=75)
    b = model.round_trip(buy_price=10.0, sell_price=200.0, quantity=75)
    assert a.stamp_duty > b.stamp_duty


def test_gst_applies_to_brokerage_and_fees_but_not_to_stt():
    """GST on STT would be a tax on a tax. Charging it inflates every trade
    by a few rupees, which on a hundred trades is a real number."""
    model = CostModel(gst_pct=18.0)
    breakdown = model.round_trip(buy_price=100.0, sell_price=100.0, quantity=75)
    taxable = (breakdown.brokerage + breakdown.exchange
               + breakdown.sebi + breakdown.ipft)
    assert breakdown.gst == pytest.approx(taxable * 0.18)


def test_costs_scale_with_premium_not_just_with_lot_count():
    """The whole reason a flat charge is the wrong shape. A cheap
    out-of-the-money trade must not be charged like an expensive one."""
    model = CostModel()
    cheap = model.round_trip(buy_price=20.0, sell_price=25.0, quantity=75)
    rich = model.round_trip(buy_price=400.0, sell_price=420.0, quantity=75)
    assert rich.total > cheap.total * 2


def test_costs_scale_with_quantity():
    model = CostModel()
    one_lot = model.round_trip(buy_price=100.0, sell_price=110.0, quantity=75)
    four_lots = model.round_trip(buy_price=100.0, sell_price=110.0, quantity=300)
    assert four_lots.total > one_lot.total


def test_nothing_is_ever_negative():
    """A negative charge is a credit, and a backtest that pays you to trade
    will find an enormous edge that does not exist."""
    for buy, sell, qty in ((0.0, 0.0, 75), (0.05, 0.0, 75), (500.0, 0.05, 300)):
        breakdown = CostModel().round_trip(buy, sell, qty)
        for name, value in breakdown.to_dict().items():
            if isinstance(value, (int, float)):
                assert value >= 0, f"{name} went negative"


def test_the_dominant_charge_is_named():
    """Knowing the total is ₹400 is less useful than knowing ₹300 of it was
    STT, which is a fact you can act on by holding to expiry differently."""
    breakdown = CostModel().round_trip(buy_price=150.0, sell_price=300.0, quantity=300)
    assert breakdown.largest == "stt"


def test_a_flat_model_still_works_for_the_index_engine():
    """The index backtest trades points, not premium, so it has no turnover
    to charge a percentage against."""
    breakdown = FlatCostModel(per_round_trip=120.0).round_trip(100.0, 110.0, 75)
    assert breakdown.total == 120.0


# ---- slippage ---------------------------------------------------------

def test_slippage_always_moves_the_fill_against_you():
    model = SlippageModel()
    assert buy_fill(100.0, model).filled > 100.0
    assert sell_fill(100.0, model).filled < 100.0


def test_a_sell_can_never_fill_below_zero():
    """Without the floor, a near-worthless option plus slippage produces a
    negative exit price — which shows up as a profit on a losing trade."""
    fill = sell_fill(0.05, SlippageModel(ticks=10))
    assert fill.filled == 0.0


def test_a_stored_spread_is_used_and_labelled_as_measured():
    """The reason `option_candles` carries bid and ask at all: the day a
    source provides them, slippage stops being an assumption."""
    model = SlippageModel(spread_fraction=0.5)
    fill = buy_fill(100.0, model, bid=99.0, ask=101.0)
    assert fill.slippage == pytest.approx(1.0)
    assert fill.basis == "measured_spread"


def test_a_missing_spread_falls_back_to_ticks_and_says_so():
    """NSE's public chain publishes no depth, so this is the normal case —
    and it must not be mistaken for a measurement."""
    fill = buy_fill(100.0, SlippageModel(ticks=2, tick_size=0.05))
    assert fill.slippage == pytest.approx(0.10)
    assert fill.basis == "assumed_ticks"


def test_a_crossed_or_absurd_quote_falls_back_rather_than_trusting_it():
    """A bid above the ask is a stale or broken quote. Using it would
    produce negative slippage — a fill better than the market."""
    fill = buy_fill(100.0, SlippageModel(), bid=105.0, ask=95.0)
    assert fill.basis == "assumed_ticks"
    assert fill.slippage > 0


def test_index_slippage_is_proportional_to_the_index_level():
    model = SlippageModel(index_pct=0.02)
    assert model.index_points(24_000) == pytest.approx(4.8)


# ---- reporting --------------------------------------------------------

def test_assumptions_are_described_for_the_result_block():
    """A backtest that does not state its cost assumptions cannot be
    compared with one that used different ones, and in a month you will not
    remember which was which."""
    described = describe(CostModel(), SlippageModel())
    assert described["costs"]["kind"] == "itemised"
    assert described["costs"]["rates_as_of"] == "2024-10"
    assert "contract note" in described["caveat"]

    flat = describe(FlatCostModel(), SlippageModel())
    assert flat["costs"]["kind"] == "flat"


# ---- annualisation ----------------------------------------------------

def test_sharpe_annualisation_uses_the_real_trade_rate():
    """The equity curve gains a point per trade, not per day. Annualising it
    by root-252 treated every trade as taking exactly one day, which
    inflates the ratio for a strategy holding twenty minutes and deflates it
    for one holding a week — so the published Sharpe was not comparable to
    anything, including its own earlier runs."""
    from app.backtest.engine import Trade, _annualisation

    def trade(entry, exit_):
        return Trade(entry_time=entry, exit_time=exit_, side="BUY", entry=100.0,
                     exit=101.0, quantity=75, stop_loss=99.0, target=103.0,
                     pnl=75.0, r_multiple=1.0, exit_reason="target",
                     confidence=0.6)

    # 50 trades across roughly a year should annualise at about 50, not 252.
    trades = [trade("2026-01-05T04:00:00+00:00", "2026-01-05T05:00:00+00:00")] + \
             [trade("2026-06-05T04:00:00+00:00", "2026-06-05T05:00:00+00:00")] * 48 + \
             [trade("2026-12-28T04:00:00+00:00", "2026-12-28T05:00:00+00:00")]
    per_year, basis = _annualisation(trades)
    assert 45 < per_year < 60
    assert "trades/year" in basis


def test_a_span_too_short_to_annualise_refuses_to_extrapolate():
    """Three days of trading does not support an annual figure, and
    scaling one week into a year manufactures confidence out of nothing."""
    from app.backtest.engine import Trade, _annualisation

    trades = [
        Trade("2026-06-16T04:00:00+00:00", "2026-06-16T05:00:00+00:00", "BUY",
              100.0, 101.0, 75, 99.0, 103.0, 75.0, 1.0, "target", 0.6),
        Trade("2026-06-17T04:00:00+00:00", "2026-06-17T05:00:00+00:00", "BUY",
              100.0, 101.0, 75, 99.0, 103.0, 75.0, 1.0, "target", 0.6),
    ]
    per_year, basis = _annualisation(trades)
    assert per_year == 1.0
    assert "too short" in basis


def test_a_single_trade_is_not_annualised_at_all():
    from app.backtest.engine import Trade, _annualisation

    one = [Trade("2026-06-16T04:00:00+00:00", "2026-06-16T05:00:00+00:00", "BUY",
                 100.0, 101.0, 75, 99.0, 103.0, 75.0, 1.0, "target", 0.6)]
    per_year, basis = _annualisation(one)
    assert per_year == 1.0
    assert "too few trades" in basis
