"""The option-buying engine, end to end.

The signal and the plan are injected here rather than generated. That is
deliberate and it is not a mock of the strategy: the engine's job is to turn
an already-made decision into a contract, a size, a fill and a labelled
premium, and a test that also had to coax `signal_engine` into producing a
BUY would be asserting on the signal engine's behaviour by accident. The
seams exist for exactly this, and the defaults remain the live engines.

What is *not* injected is everything under test: contract selection, the
risk manager's veto, the pricing policy, the cost model, the exits, and both
look-ahead guards.
"""
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import plan as plan_builder
from app.analytics import signal_engine
from app.backtest.costs import SlippageModel
from app.optionbuy import strategy
from app.optionbuy.chain import ChainStore, ContractKey, OptionLookaheadError
from app.optionbuy.contracts import SelectionConfig
from app.optionbuy.pricing import (
    MODELLED,
    MODELLED_ONLY,
    OBSERVED_ONLY,
    PREFER_OBSERVED,
    SNAPSHOT_DERIVED,
)
from app.optionbuy.strategy import OptionBuyConfig, run
from app.risk.manager import RiskConfig
from optionbuy_fixtures import candles, sessions, store_for

DAYS = sessions(date(2025, 6, 2), 3)
EXPIRY = date(2025, 6, 10)
BASE = 24_000.0


def frame(days=None, drift=1.5, **kwargs):
    return candles(days or DAYS, base=BASE, drift=drift, **kwargs)


def a_signal(action="BUY", *, stop_points=40.0, target_points=120.0,
             confidence=0.62):
    """A fixed decision, so the test states the market it means."""
    def build(window):
        price = float(window["close"].iloc[-1])
        sign = 1 if action == "BUY" else -1
        return signal_engine.Signal(
            symbol="NIFTY", timeframe="5m",
            timestamp=window["timestamp"].iloc[-1].isoformat(),
            action=action, confidence=confidence, price=price,
            entry=price,
            stop_loss=price - sign * stop_points,
            target=price + sign * target_points,
            checks=[signal_engine.Check("trend", 1.0, 0.3, "test")],
        )
    return build


def a_plan(bias=plan_builder.BULLISH, state=plan_builder.ENTER_NOW,
           regime_day="TREND_UP", regime_hour="TREND_UP"):
    def build(window):
        return plan_builder.Plan(
            symbol="NIFTY", timeframe="5m",
            timestamp=window["timestamp"].iloc[-1].isoformat(),
            bias={"label": bias, "confidence": 0.7},
            entry={"state": state, "confidence": 0.6,
                   "regime_day": regime_day, "regime_hour": regime_hour},
        )
    return build


def go(candle_frame=None, store=None, *, policy=PREFER_OBSERVED,
       config=None, selection=None, signal=None, plan=None, risk=None,
       **kwargs):
    candle_frame = frame() if candle_frame is None else candle_frame
    cfg = config or OptionBuyConfig(pricing_policy=policy, warmup=20, **kwargs)
    if store is None and policy != MODELLED_ONLY:
        store = store_for(DAYS, EXPIRY, candle_frame)
    return run(
        candle_frame,
        store=store,
        config=cfg,
        selection=selection or SelectionConfig(min_premium=0.5),
        risk_config=risk,
        signal_fn=signal or a_signal(),
        plan_fn=plan or a_plan(),
    )


# ---- it consumes the existing decision, it does not make one -----------

def test_a_hold_never_becomes_a_trade():
    result = go(signal=a_signal("HOLD"))
    assert result.trades == []
    assert result.rejections["counts"][strategy.HOLD_SIGNAL] > 0


def test_an_entry_state_that_is_not_ready_blocks_the_trade():
    """Direction and timing fail independently. That is why the desk splits
    them, and the backtest has to respect the split or it measures the
    single-verdict strategy the platform already moved away from."""
    result = go(plan=a_plan(state=plan_builder.WAIT_PULLBACK))
    assert result.trades == []
    counts = result.rejections["counts"]
    assert counts[strategy.ENTRY_STATE_BLOCKED] > 0
    assert "WAIT_PULLBACK" in result.rejections["examples"][
        strategy.ENTRY_STATE_BLOCKED]


def test_a_bias_pointing_the_other_way_blocks_the_trade():
    result = go(plan=a_plan(bias=plan_builder.BEARISH))
    assert result.trades == []
    assert result.rejections["counts"][strategy.BIAS_DISAGREES] > 0


def test_relaxing_the_gate_is_visible_in_the_assumptions():
    """A study that loosened the entry convention must not be readable as a
    run of the desk's actual rules."""
    result = go(config=OptionBuyConfig(
        warmup=20, require_entry_states=(), require_bias_agreement=False))
    assert result.assumptions["run"]["require_entry_states"] == []
    assert result.assumptions["run"]["require_bias_agreement"] is False
    assert result.trades


def test_the_decision_travels_onto_the_trade():
    result = go()
    trade = result.trades[0]
    assert trade.bias == plan_builder.BULLISH
    assert trade.entry_state == plan_builder.ENTER_NOW
    assert trade.regime_hour == "TREND_UP"
    assert trade.confidence == pytest.approx(0.62)
    assert trade.checks == {"trend": pytest.approx(0.3)}


# ---- the existing entry convention ------------------------------------

def test_entry_is_the_next_bar_open_not_this_bar_close():
    import pandas as pd

    data = frame()
    trade = go(data).trades[0]

    fill_bar = data.index[data["timestamp"] == pd.Timestamp(trade.entry_time)]
    assert len(fill_bar) == 1
    position = int(fill_bar[0])
    assert trade.index_entry == pytest.approx(float(data["open"].iloc[position]))
    # The decision was made on the bar before, and could not have been
    # filled on it — `walk` never yields the last bar for the same reason.
    assert position > 0


def test_a_gap_past_the_stop_is_skipped_rather_than_sized():
    """Not a trade with tiny risk — a trade whose premise is already invalid.
    Clamping the risk instead once turned one trade into 654 lots."""
    data = frame(drift=1.5)
    # A stop above the next open for a BUY: the fill is already through it.
    result = go(data, signal=a_signal("BUY", stop_points=-500.0))
    assert result.trades == []
    assert result.rejections["counts"][strategy.GAP_PAST_STOP] > 0


# ---- the risk manager keeps its veto ----------------------------------

def test_the_daily_trade_cap_is_the_risk_managers_and_still_applies():
    result = go(risk=RiskConfig(capital=200_000, max_trades_per_day=1),
                config=OptionBuyConfig(warmup=20, max_bars_in_trade=2,
                                       pricing_policy=PREFER_OBSERVED))
    by_day = {}
    for trade in result.trades:
        day = trade.entry_time[:10]
        by_day[day] = by_day.get(day, 0) + 1
    assert by_day and max(by_day.values()) == 1
    assert result.rejections["counts"].get(strategy.RISK_VETO)


def test_the_kill_switch_stops_every_entry():
    result = go(risk=RiskConfig(capital=200_000, kill_switch=True))
    assert result.trades == []
    assert "Kill switch" in result.rejections["examples"][strategy.RISK_VETO]


def test_a_reward_risk_below_the_floor_is_vetoed():
    result = go(signal=a_signal("BUY", stop_points=100.0, target_points=110.0),
                risk=RiskConfig(capital=200_000, min_risk_reward=2.0))
    assert result.trades == []
    assert "Reward:risk" in result.rejections["examples"][strategy.RISK_VETO]


def test_the_risk_decision_is_stored_on_the_trade():
    """Auditability: a size with no decision behind it cannot be argued
    about six months later."""
    trade = go().trades[0]
    assert trade.risk["approved"] is True
    assert trade.risk["quantity"] == trade.quantity
    assert trade.quantity % 75 == 0
    assert trade.risk["risk_amount"] == pytest.approx(
        trade.quantity * trade.risk["risk_per_unit"])
    actual_pct = trade.risk["risk_amount"] / 200_000 * 100
    assert any(f"Risking {actual_pct:.2f}%" in r for r in trade.risk["reasons"])


def test_sizing_is_in_premium_terms_not_index_points():
    """Index-point sizing overstates an option buyer's risk by roughly the
    inverse of delta, which is how a 1% rule becomes a 2% loss."""
    trade = go().trades[0]
    premium_risk = trade.premium_entry - trade.premium_stop
    assert premium_risk > 0
    # The reported premiums are rounded to paise; the sizing used the full
    # precision, so this compares to within one rupee across the whole lot.
    assert trade.risk_amount == pytest.approx(premium_risk * trade.quantity, abs=1.0)
    index_risk = abs(trade.index_entry - trade.index_stop)
    assert premium_risk < index_risk


def test_a_premium_risk_too_small_to_size_against_is_refused():
    result = go(signal=a_signal("BUY", stop_points=0.01, target_points=120.0))
    assert result.trades == []
    assert result.rejections["counts"][strategy.NO_DEFINED_RISK] > 0


def test_the_stop_projection_is_time_consistent_with_the_entry_premium():
    """An observed entry price comes from the decision bar; projecting the
    stop at the fill tenor instead would make five minutes of time value
    look like risk. A one-point stop must therefore report a risk of about
    one point of delta, not ten rupees of decay.
    """
    observed = go(policy=PREFER_OBSERVED,
                  signal=a_signal("BUY", stop_points=1.0, target_points=200.0))
    modelled = go(policy=MODELLED_ONLY, store=None,
                  signal=a_signal("BUY", stop_points=1.0, target_points=200.0))

    for result in (observed, modelled):
        # Too small a risk to size against, under either policy — the
        # rejection is the stop being one point away, never the clock.
        assert result.trades == []
        assert result.rejections["counts"][strategy.NO_DEFINED_RISK] > 0


# ---- pricing evidence, end to end -------------------------------------

def test_an_archive_run_labels_its_fills_snapshot_derived():
    result = go(policy=PREFER_OBSERVED)
    assert result.trades
    assert all(t.entry_evidence == SNAPSHOT_DERIVED for t in result.trades)
    assert result.evidence["observed_pct"] == 100.0


def test_every_observed_fill_cites_the_row_it_came_from():
    trade = go().trades[0]
    reference = trade.entry_quote["reference"]
    assert reference["option_candle_id"]
    assert reference["contract"].endswith(EXPIRY.isoformat())
    assert reference["bar_timestamp"]
    assert reference["bar_kind"] == "snapshot"


def test_a_modelled_run_says_so_on_every_trade_and_in_the_limitations():
    result = go(policy=MODELLED_ONLY, store=None)
    assert result.trades
    assert all(t.evidence == MODELLED for t in result.trades)
    assert result.evidence["observed_pct"] == 0.0
    assert any("Black-Scholes at a constant IV" in line
               for line in result.limitations)


def test_observed_only_with_no_archive_takes_nothing_and_names_the_reason():
    """The refusal chain runs selection-first, so an empty archive is caught
    as "no expiry listed" rather than as an unpriceable contract. Either
    way it is a named code and never a quiet fallback to the model."""
    from app.optionbuy import contracts as contract_module

    result = go(policy=OBSERVED_ONLY, store=ChainStore({}, {}))
    assert result.trades == []
    assert result.rejections["counts"][contract_module.NO_EXPIRY] > 0
    assert "option archive" in result.rejections["examples"][
        contract_module.NO_EXPIRY]


def test_a_contract_that_survives_selection_but_cannot_be_priced_is_refused():
    """The narrow path: the chain listed it, liquidity let it through, and
    the quote is not a tradable price. Under observed_only that is a
    rejection, not a modelled fill."""
    data = frame()
    store = store_for(DAYS, EXPIRY, data)
    for key in list(store._bars):                        # noqa: SLF001
        store._bars[key] = [                             # noqa: SLF001
            type(b)(**{**b.__dict__, "close": 0.0}) for b in store._bars[key]]

    result = go(data, store, policy=OBSERVED_ONLY,
                selection=SelectionConfig(min_premium=0.0))
    assert result.trades == []
    assert result.rejections["counts"][strategy.UNPRICEABLE] > 0
    assert "does not permit a modelled premium" in result.rejections[
        "examples"][strategy.UNPRICEABLE]


def test_the_evidence_gate_refuses_a_mostly_modelled_result():
    result = go(policy=MODELLED_ONLY, store=None,
                config=OptionBuyConfig(warmup=20, pricing_policy=MODELLED_ONLY,
                                       min_observed_pct=90.0))
    assert result.refused is not None
    assert result.refused["error"] == "insufficient observed pricing"
    assert result.trades          # the run happened; the result is refused


def test_a_mixed_trade_is_labelled_mixed_rather_than_the_better_half():
    """Entry from the archive, exit from a bar the collector missed."""
    data = frame()
    store = store_for(DAYS, EXPIRY, data)
    # Drop every quote after the first session's first hour, so exits fall
    # into the hole while entries do not.
    cutoff = data["timestamp"].iloc[14].to_pydatetime()
    for key in list(store._bars):                        # noqa: SLF001
        store._bars[key] = [b for b in store._bars[key]  # noqa: SLF001
                            if b.timestamp <= cutoff]
        store._stamps[key] = [b.timestamp for b in store._bars[key]]  # noqa: SLF001

    result = go(data, store,
                config=OptionBuyConfig(warmup=10, max_bars_in_trade=6,
                                       pricing_policy=PREFER_OBSERVED))
    mixed = [t for t in result.trades if t.evidence == "MIXED"]
    assert mixed
    first = mixed[0]
    assert first.entry_evidence == SNAPSHOT_DERIVED
    assert first.exit_evidence == MODELLED
    assert result.evidence["counts"]["MIXED"] == len(mixed)


# ---- costs and slippage ------------------------------------------------

def test_costs_are_itemised_and_subtracted_from_the_gross():
    trade = go().trades[0]
    assert trade.costs["total"] > 0
    assert trade.pnl == pytest.approx(trade.gross_pnl - trade.execution_friction - trade.costs["total"],
                                      abs=0.01)
    # For a retail option buyer the dominant charge is usually STT on the
    # sell leg, and that is only actionable if it is broken out.
    assert set(trade.costs) >= {"brokerage", "stt", "exchange", "gst", "largest"}


def test_slippage_makes_the_buy_worse_and_the_sell_worse():
    quiet = run(frame(), store=store_for(DAYS, EXPIRY, frame()),
                config=OptionBuyConfig(warmup=20),
                selection=SelectionConfig(min_premium=0.5),
                slippage_model=SlippageModel(ticks=0.0),
                signal_fn=a_signal(), plan_fn=a_plan())
    slipped = run(frame(), store=store_for(DAYS, EXPIRY, frame()),
                  config=OptionBuyConfig(warmup=20),
                  selection=SelectionConfig(min_premium=0.5),
                  slippage_model=SlippageModel(ticks=20.0),
                  signal_fn=a_signal(), plan_fn=a_plan())

    assert slipped.trades[0].premium_entry > quiet.trades[0].premium_entry
    assert slipped.trades[0].premium_exit < quiet.trades[0].premium_exit


def test_a_quoted_spread_makes_slippage_measured_rather_than_assumed():
    data = frame()
    store = store_for(DAYS, EXPIRY, data, bid_ask_spread=4.0)
    result = go(data, store)
    assert result.trades
    assert result.trades[0].entry_quote["premium"] > 0


def test_the_cost_drag_is_reported_alongside_the_net_number():
    stats = go().stats
    assert stats["total_costs"] > 0
    assert stats["expectancy_per_trade_before_costs"] != stats[
        "expectancy_per_trade"]
    assert stats["gross_pnl"] - stats["total_costs"] == pytest.approx(
        stats["net_pnl"], abs=0.5)


# ---- exits --------------------------------------------------------------

def test_the_stop_is_assumed_to_fill_first_when_a_bar_touches_both():
    """No way to know which came first inside a five-minute bar. Assuming
    the good one is how a backtest flatters itself."""
    data = frame(drift=0.0, wick=400.0)
    result = go(data, signal=a_signal("BUY", stop_points=50.0,
                                      target_points=100.0))
    assert result.trades
    assert result.trades[0].exit_reason == strategy.STOP


def test_a_position_is_closed_before_the_session_ends():
    result = go()
    for trade in result.trades:
        assert trade.entry_time[:10] == trade.exit_time[:10] or \
            trade.exit_reason in (strategy.SESSION_END, strategy.SESSION_BOUNDARY)


def test_nothing_is_held_across_a_session_boundary_by_default():
    """The overnight gap is not a five-minute move, and an option held
    through it decays for seventeen hours the strategy never intended."""
    from app.market_hours import IST

    result = go()
    for trade in result.trades:
        entry_day = datetime.fromisoformat(trade.entry_time).astimezone(IST).date()
        exit_day = datetime.fromisoformat(trade.exit_time).astimezone(IST).date()
        assert entry_day == exit_day


def test_the_session_boundary_guard_fires_when_the_time_rule_is_disabled():
    from datetime import time as clock

    result = go(config=OptionBuyConfig(
        warmup=20, pricing_policy=PREFER_OBSERVED,
        max_bars_in_trade=10_000, session_exit_ist=clock(23, 59)))

    assert any(t.exit_reason == "session_or_data_boundary" for t in result.trades)


def test_an_expired_contract_is_settled_rather_than_held():
    """Expiry beats every other exit rule. A contract that has expired
    cannot be held whatever the stop and target are doing."""
    days = sessions(date(2025, 6, 2), 3)
    data = frame(days)
    expiry = days[1]                       # expiry lands mid-window
    store = store_for(days, expiry, data)

    result = run(data, store=store,
                 config=OptionBuyConfig(warmup=20, max_bars_in_trade=10_000,
                                        hold_overnight=True,
                                        session_exit_ist=__import__(
                                            "datetime").time(23, 59)),
                 selection=SelectionConfig(min_premium=0.5,
                                           min_days_to_expiry=0.0),
                 signal_fn=a_signal("BUY", stop_points=5_000.0,
                                    target_points=5_000.0),
                 plan_fn=a_plan())

    reasons = {t.exit_reason for t in result.trades}
    assert strategy.EXPIRY in reasons


def test_the_exit_basis_says_whether_the_level_or_the_bar_was_priced():
    """An OBSERVED exit fills at the close of the bar that triggered, not at
    the trigger level — the archive has no intrabar option prices. Hiding
    that difference would make two exit styles look like one."""
    observed = go(policy=PREFER_OBSERVED).trades[0]
    modelled = go(policy=MODELLED_ONLY, store=None).trades[0]

    assert observed.exit_basis == "bar_close_observed"
    assert modelled.exit_basis in ("trigger_level", "bar_close")
    assert any("exit_basis" in line for line in go().limitations)


# ---- look-ahead ---------------------------------------------------------

def test_the_option_clock_never_runs_ahead_of_the_candle_walk():
    """Not asserted by inspection — the store raises, so a leak stops the
    run instead of producing a slightly better fill."""
    data = frame()
    store = store_for(DAYS, EXPIRY, data)
    result = go(data, store)
    assert result.trades
    # Every quote cited belongs to a bar at or before the fill that used it.
    for trade in result.trades:
        cited = trade.entry_quote["reference"]["bar_timestamp"]
        assert cited <= trade.entry_time


def test_a_strategy_reaching_past_the_walk_is_stopped_not_flattered():
    data = frame()
    store = store_for(DAYS, EXPIRY, data)
    store.advance(data["timestamp"].iloc[30].to_pydatetime())
    with pytest.raises(OptionLookaheadError):
        store.bar_at(ContractKey(EXPIRY, 24_000.0, "CE"),
                     data["timestamp"].iloc[31].to_pydatetime())


def test_the_walk_reserves_the_last_bar_so_every_fill_could_have_happened():
    data = frame()
    result = go(data)
    last = data["timestamp"].iloc[-1].isoformat()
    assert all(t.entry_time <= last for t in result.trades)


# ---- the result explains itself ----------------------------------------

def test_the_result_carries_stats_breakdowns_and_limitations():
    result = go()
    payload = result.to_dict()

    assert payload["strategy"] == "option_buying"
    assert payload["stats"]["trades"] == len(result.trades)
    assert {"win_rate_pct", "expectancy_r", "max_drawdown_pct",
            "total_costs"} <= set(payload["stats"])
    assert {"by_regime_hour", "by_time_of_day", "by_expiry_distance",
            "by_confidence", "by_evidence"} <= set(payload["breakdowns"])
    assert payload["limitations"]


def test_a_run_with_no_trades_says_what_stopped_them():
    result = go(signal=a_signal("HOLD"))
    assert result.stats["trades"] == 0
    assert "rejection counts" in result.stats["note"]
    assert result.rejections["total"] > 0


def test_the_breakdowns_carry_their_own_counts():
    """A 100% win rate over two trades is not a finding, and the count is
    what stops it being read as one."""
    result = go()
    for rows in result.breakdowns.values():
        for row in rows:
            assert row["trades"] >= 1
            if row["trades"] < 10:
                assert row["note"] == "Too few trades to read."


def test_every_trade_names_the_contract_it_bought():
    trade = go().trades[0]
    assert trade.option_type == "CE"
    assert trade.strike > 0
    assert trade.expiry == EXPIRY.isoformat()
    assert trade.contract.startswith(f"{trade.strike:.0f} CE")
    assert trade.days_to_expiry > 0
    assert trade.moneyness in ("ATM", "ITM", "OTM")


def test_a_sell_signal_buys_a_put():
    result = go(signal=a_signal("SELL"), plan=a_plan(bias=plan_builder.BEARISH))
    assert result.trades
    assert all(t.option_type == "PE" for t in result.trades)


def test_decay_is_measured_against_the_clock_and_nothing_else():
    """Both sides of the decay figure are modelled at the same index level
    and the same IV. Comparing a modelled price against the observed exit
    instead would fold model-versus-market error into a number labelled
    "decay" — and on an observed run that error is the larger of the two.
    """
    from app.analytics import option_pricing

    trade = go().trades[0]
    frozen = option_pricing.price(
        trade.index_exit, trade.strike, YEARS_AT_ENTRY(trade),
        trade.sizing_iv, kind=trade.option_type)
    assert trade.decay_cost == pytest.approx(
        (frozen - option_pricing.price(
            trade.index_exit, trade.strike, YEARS_AT_EXIT(trade),
            trade.sizing_iv, kind=trade.option_type)) * trade.quantity,
        abs=0.01)
    # Time only runs one way, so holding a long option can never be paid
    # for the passage of time.
    assert trade.decay_cost >= 0


def YEARS_AT_ENTRY(trade):
    from app.analytics import option_pricing
    from app.market_hours import IST
    from app.optionbuy.contracts import expiry_moment

    entry = datetime.fromisoformat(trade.entry_time).astimezone(IST)
    return option_pricing.years_to_expiry(
        entry, expiry_moment(date.fromisoformat(trade.expiry)))


def YEARS_AT_EXIT(trade):
    from app.analytics import option_pricing
    from app.market_hours import IST
    from app.optionbuy.contracts import expiry_moment

    exit_at = datetime.fromisoformat(trade.exit_time).astimezone(IST)
    return option_pricing.years_to_expiry(
        exit_at, expiry_moment(date.fromisoformat(trade.expiry)))


def test_a_decision_on_the_last_bar_of_a_session_is_not_filled_next_morning():
    """It would fill seventeen hours later, through an overnight gap,
    against levels computed from a bar that is no longer the last one — and
    the option would pay a night of decay for a signal nobody could act on.
    """
    from app.market_hours import IST

    result = go()
    assert result.rejections["counts"][strategy.FILL_CROSSES_SESSION] > 0

    for trade in result.trades:
        decision = datetime.fromisoformat(trade.entry_time).astimezone(IST)
        assert decision.date() == datetime.fromisoformat(
            trade.exit_time).astimezone(IST).date()


def test_a_trade_counts_against_the_day_it_was_actually_filled():
    """Because the fill can no longer land in a different session than the
    decision, the daily cap and the realised P&L cannot drift onto two
    different days' risk state."""
    from app.market_hours import IST

    result = go(risk=RiskConfig(capital=200_000, max_trades_per_day=2),
                config=OptionBuyConfig(warmup=20, max_bars_in_trade=2))
    per_day = {}
    for trade in result.trades:
        day = datetime.fromisoformat(trade.entry_time).astimezone(IST).date()
        per_day[day] = per_day.get(day, 0) + 1
    assert per_day
    assert max(per_day.values()) <= 2


# ---- Repair Pass 2B.1: the position does not exist before it is filled ----

def test_the_stored_entry_index_is_the_delayed_execution_bar():
    """`entry_index` was `i + 1` whatever the latency, so the bar count, the
    time cap and the session-exit check all began running on a bar the
    order had not reached. Every one of those is measured from the bar the
    position actually opened on."""
    import pandas as pd

    from app.backtest.execution import ExecutionPolicy

    bars = frame()
    result = go(bars, config=OptionBuyConfig(
        warmup=20, pricing_policy=PREFER_OBSERVED,
        execution_policy=ExecutionPolicy(latency_seconds=900)))

    assert result.trades
    stamps = list(bars["timestamp"])
    for trade in result.trades:
        timing = trade.timing
        signal_time = pd.Timestamp(timing["bar_close_time"])
        earliest = pd.Timestamp(timing["earliest_execution_time"])
        filled = pd.Timestamp(timing["actual_fill_time"])

        assert earliest == signal_time + pd.Timedelta(seconds=900)
        assert filled >= earliest
        assert pd.Timestamp(trade.entry_time) == filled
        # The fill is a real bar open, and the bars the order could not
        # have reached were skipped rather than priced.
        assert filled in stamps
        assert pd.Timestamp(trade.exit_time) > filled
        # `bars_held` is `exit_index - entry_index + 1`, so it is the stored
        # entry index made visible. Counted from the fill bar it matches the
        # bars between the fill and the exit; counted from `i + 1` it comes
        # out three too many at this latency, and the trade claims to have
        # been open before it was filled.
        exit_bar_open = pd.Timestamp(trade.exit_time) - pd.Timedelta(minutes=5)
        expected = stamps.index(exit_bar_open) - stamps.index(filled) + 1
        assert trade.bars_held == expected


def test_a_long_latency_no_longer_moves_the_position_clock_backwards():
    """The reproduction, made deterministic.

    `max_bars_in_trade=0` makes the trade close on the first bar management
    looks at. With `entry_index` stored as `i + 1`, that was the bar right
    after the *decision* — three bars before the fill — so the ledger
    stamped a CLOSE at that bar's close and then found it was earlier than
    the OPEN it had already stamped at the fill. `PositionLedger.move`
    refused it: `position clock moved backwards`.

    With the entry index being the bar the position actually opens on, and
    management held back until the walk reaches it, the first bar
    management sees *is* the fill bar. The trade closes there, and the
    clock runs forwards.
    """
    import pandas as pd

    from app.backtest.execution import ExecutionPolicy

    result = go(config=OptionBuyConfig(
        warmup=20, pricing_policy=PREFER_OBSERVED, max_bars_in_trade=0,
        execution_policy=ExecutionPolicy(latency_seconds=900)))

    assert result.trades
    positions = result.dataset["positions"]
    assert positions["capital_reconciled"] is True
    assert positions["entries"] == positions["closed_positions"] == len(result.trades)

    stamps = [pd.Timestamp(event["timestamp"]) for event in positions["events"]]
    assert stamps == sorted(stamps)

    for trade in result.trades:
        # Closed on the fill bar, not three bars before it.
        assert trade.exit_reason == strategy.TIME
        assert trade.bars_held == 1
        filled = pd.Timestamp(trade.timing["actual_fill_time"])
        assert pd.Timestamp(trade.entry_time) == filled
        assert pd.Timestamp(trade.exit_time) > filled


def test_an_entry_under_latency_is_not_priced_off_the_decision_bar_quote():
    """Every archived quote at or before the decision bar became available
    before the order could exist. The first eligible one is past the walk,
    so the declared fallback applies and the trade says so — rather than
    filling at a print it could never have reached."""
    from app.backtest.execution import ExecutionPolicy

    result = go(config=OptionBuyConfig(
        warmup=20, pricing_policy=PREFER_OBSERVED,
        execution_policy=ExecutionPolicy(latency_seconds=900)))

    assert result.trades
    for trade in result.trades:
        assert trade.entry_basis == "modelled_no_eligible_quote"
        assert trade.entry_evidence == MODELLED
    stated = " ".join(result.limitations)
    assert "modelled_no_eligible_quote" in stated
    assert "before the order could exist" in stated


def test_observed_only_refuses_a_latency_entry_rather_than_faking_a_quote():
    from app.backtest.execution import ExecutionPolicy

    result = go(policy=OBSERVED_ONLY, config=OptionBuyConfig(
        warmup=20, pricing_policy=OBSERVED_ONLY,
        execution_policy=ExecutionPolicy(latency_seconds=900)))

    assert result.trades == []
    assert result.rejections["counts"][strategy.NO_ELIGIBLE_QUOTE] > 0


# ---- 2B.2: eligibility is not a latency special case ---------------------

def a_store_with_one_quote(available_at):
    """A store holding a single quote for one contract, made available at a
    chosen instant. Everything else about it is irrelevant to eligibility."""
    from datetime import UTC, datetime, timedelta

    from app.optionbuy.chain import ChainStore, ContractMeta, OptionBar

    key = ContractKey(expiry=EXPIRY, strike=24_000.0, option_type="CE")
    # `available()` is max(timestamp + one bar, available_at, first_seen), so
    # the bar is stamped one bucket before the availability being tested.
    bar = OptionBar(
        row_id=1, contract_id=1, key=key,
        timestamp=available_at - timedelta(minutes=5),
        open=100.0, high=101.0, low=99.0, close=100.0, volume=10.0,
        open_interest=100.0, iv=0.15, bid=None, ask=None,
        underlying_close=24_000.0, bar_kind="ohlc", source="test",
        samples=1, session_date=available_at.date(),
        available_at=available_at)
    meta = ContractMeta(contract_id=1, key=key, lot_size=75,
                        tradingsymbol="T", source="test",
                        first_seen=datetime(2025, 1, 1, tzinfo=UTC))
    return key, ChainStore({key: [bar]}, {key: meta})


@pytest.mark.parametrize("offset_seconds,eligible", [
    (-300, False),   # became available five minutes before eligibility
    (-1, False),     # one second before — still a price the order never saw
    (0, True),       # exactly at the execution clock
    (1, True),       # after it
])
def test_quote_eligibility_is_enforced_at_zero_latency(offset_seconds, eligible):
    """The 2B.1 defect, directly.

    Zero latency means the execution clock starts at the signal instant. It
    does not mean an earlier quote becomes acceptable — and the old code
    read that second meaning into it, handing a 05:25 bucket to an order
    that could not exist before 05:30.

    The walk is at the eligibility instant, so a quote that became available
    one second later is unreachable rather than ineligible; the 0s and +1s
    cases are therefore driven by the same boundary and both must pass.
    """
    from datetime import timedelta

    from app.optionbuy import pricing as pricing_module

    eligible_at = datetime(2025, 6, 2, 5, 30, tzinfo=UTC)
    available = eligible_at + timedelta(seconds=offset_seconds)
    key, store = a_store_with_one_quote(available)
    store.seek(max(available, eligible_at))

    found = store.bar_at(key, max(available, eligible_at),
                         eligible_from=eligible_at)
    assert (found is not None) is eligible

    # observed_only: an ineligible quote is a refusal, never a stale reuse.
    if eligible:
        quoted = pricing_module.quote(
            store, key, max(available, eligible_at), spot=24_000.0,
            years=0.02, policy=OBSERVED_ONLY, eligible_from=eligible_at)
        assert quoted.evidence != MODELLED
    else:
        with pytest.raises(pricing_module.UnpriceableContract) as raised:
            pricing_module.quote(
                store, key, max(available, eligible_at), spot=24_000.0,
                years=0.02, policy=OBSERVED_ONLY, eligible_from=eligible_at)
        assert raised.value.ineligible is True

    # fallback-enabled: modelled, labelled, and never the earlier print.
    fell_back = pricing_module.quote(
        store, key, max(available, eligible_at), spot=24_000.0, years=0.02,
        policy=PREFER_OBSERVED, eligible_from=eligible_at)
    assert (fell_back.evidence == MODELLED) is not eligible


def test_an_ineligible_quote_is_counted_apart_from_a_missing_one():
    """A contract the archive cannot price is a data gap. One whose quotes
    all predate the execution clock is the clock working. Folding them into
    one rejection code would read as a shortage of data."""
    from app.backtest.execution import ExecutionPolicy

    stale = go(policy=OBSERVED_ONLY, config=OptionBuyConfig(
        warmup=20, pricing_policy=OBSERVED_ONLY,
        execution_policy=ExecutionPolicy(latency_seconds=900)))
    counts = stale.rejections["counts"]
    assert counts.get(strategy.NO_ELIGIBLE_QUOTE, 0) > 0
    assert counts.get(strategy.UNPRICEABLE, 0) == 0


def test_the_zero_latency_entry_convention_still_takes_observed_trades():
    """Rewritten in 2B.2. It used to assert `decision_bar_quote_observed`,
    which is the stale-quote exception this pass removed, and so would have
    kept the defect alive.

    What the convention actually is: the index fills at the next bar's open
    and the premium comes from a quote available at or after that instant.
    Where the archive holds one, the entry is still OBSERVED — so the fix is
    a tightening, not a blanket switch to modelled fills.
    """
    observed = go()
    assert observed.trades
    assert {t.entry_basis for t in observed.trades} <= {
        "eligible_quote_observed", "modelled_no_eligible_quote"}
    assert any(t.entry_basis == "eligible_quote_observed"
               for t in observed.trades)

    for trade in observed.trades:
        if trade.entry_basis != "eligible_quote_observed":
            continue
        reference = trade.entry_quote.get("reference") or {}
        # `available_from`, not the raw `available_at` field: the store
        # orders bars by when they actually became readable, which for a
        # five-minute bucket is one bucket after its own stamp. The raw
        # field alone made a correctly eligible fill cite an availability
        # five minutes before its own execution clock, so the citation
        # could not be checked against the clock it had satisfied.
        available = reference["available_from"]
        assert pd.Timestamp(available) >= pd.Timestamp(
            trade.timing["earliest_execution_time"])


def test_the_option_run_says_what_its_drawdown_is_measured_on():
    stats = go().stats
    assert "max_drawdown_pct" in stats
    basis = stats["drawdown_basis"]
    assert "non-overlapping" in basis and "not daily mark-to-market" in basis
