"""One execution truth, checked by hand.

Repair Pass 2B. Four engines had four answers to the same four questions
about how a decision becomes a trade — where the levels sit after a gapped
fill, what a stop that gapped through fills at, which level filled first
when one bar covered both, and which leg was the buy. This suite fixes the
answers with arithmetic anyone can redo on paper, because the failures being
guarded against are all of the kind where the code is self-consistent and
wrong.

Several numbers here are written out longhand rather than computed. That is
deliberate: a test that recomputes the thing it is testing agrees with any
bug the implementation has.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import signal_engine
from app.backtest import costs as costs_module
from app.backtest import engine, execution, sensitivity
from app.backtest.costs import CostModel, FlatCostModel, SlippageModel, describe
from app.backtest.execution import ExecutionPolicy
from app.backtest.feed import HistoricalFeed
from app.market_hours import IST
from app.risk.manager import RiskConfig

DAY = date(2026, 6, 17)


# ---------------------------------------------------------------------------
# CA-1 / CA-2: the money, by hand
# ---------------------------------------------------------------------------

def hand_charges(buy: float, sell: float, quantity: int = 75) -> float:
    """The October-2024 F&O schedule, written out longhand.

    Independent of `CostModel` on purpose. If both were derived from the
    same expression this would prove only that the expression is stable.
    """
    buy_turnover = buy * quantity
    sell_turnover = sell * quantity
    turnover = buy_turnover + sell_turnover
    brokerage = 20.0 * 2
    stt = sell_turnover * 0.001            # 0.10%, sell leg only
    exchange = turnover * 0.0003503        # 0.03503%, both legs
    sebi = turnover * 0.000001             # 0.0001%
    ipft = turnover * 0.000005             # 0.0005%
    stamp = buy_turnover * 0.00003         # 0.003%, buy leg only
    gst = (brokerage + exchange + sebi + ipft) * 0.18
    return brokerage + stt + exchange + sebi + ipft + stamp + gst


def test_a_winning_option_round_trip_costs_what_the_schedule_says():
    """Buy 75 at 100, sell 75 at 120. Gross 1,500 before charges."""
    money = execution.account(
        entry_side="BUY", entry_price=100.0, exit_price=120.0,
        quantity=75, costs=CostModel())

    assert money.buy_price == 100.0
    assert money.sell_price == 120.0
    assert money.gross_pnl == pytest.approx(1_500.0)

    expected = hand_charges(buy=100.0, sell=120.0)
    assert expected == pytest.approx(63.362161, abs=1e-6)
    assert money.total_fees == pytest.approx(expected, abs=1e-6)
    assert money.brokerage == pytest.approx(40.0)
    assert money.statutory_fees == pytest.approx(expected - 40.0, abs=1e-6)
    assert money.net_pnl == pytest.approx(1_500.0 - expected, abs=1e-6)


def test_a_losing_round_trip_does_not_have_its_legs_reversed():
    """Buy 75 at 120, sell 75 at 100 — a 1,500 loss.

    The bug this replaces inferred the legs with `min`/`max`, which on a
    losing long names the *entry* the sale. STT then falls on 120 instead of
    100 and stamp duty on 100 instead of 120, and the bill comes out too
    small: 63.362161 where the correct answer is 61.907161. A losing trade
    was charged less than the identical winning one.
    """
    money = execution.account(
        entry_side="BUY", entry_price=120.0, exit_price=100.0,
        quantity=75, costs=CostModel())

    assert money.entry_side == "BUY" and money.exit_side == "SELL"
    assert money.buy_price == 120.0      # the entry, because it was bought
    assert money.sell_price == 100.0     # the exit, because it was sold
    assert money.gross_pnl == pytest.approx(-1_500.0)

    correct = hand_charges(buy=120.0, sell=100.0)
    reversed_legs = hand_charges(buy=100.0, sell=120.0)
    assert correct == pytest.approx(61.907161, abs=1e-6)
    assert reversed_legs == pytest.approx(63.362161, abs=1e-6)
    assert correct != pytest.approx(reversed_legs, abs=1e-6)

    assert money.total_fees == pytest.approx(correct, abs=1e-6)
    assert money.net_pnl == pytest.approx(-1_500.0 - correct, abs=1e-6)


def test_a_short_pays_stt_on_the_opening_leg():
    """Sold to open at 120, bought back at 100. The sale is the *entry*."""
    money = execution.account(
        entry_side="SELL", entry_price=120.0, exit_price=100.0,
        quantity=75, costs=CostModel())

    assert money.entry_side == "SELL" and money.exit_side == "BUY"
    assert money.sell_price == 120.0
    assert money.buy_price == 100.0
    assert money.gross_pnl == pytest.approx(1_500.0)   # a profitable short
    assert money.total_fees == pytest.approx(hand_charges(buy=100.0, sell=120.0),
                                             abs=1e-6)


def test_gross_minus_friction_minus_fees_is_net():
    """The three reported parts must still add up to the reported whole."""
    money = execution.account(
        entry_side="BUY", entry_price=101.0, exit_price=118.0,
        quantity=75, costs=CostModel(),
        reference_entry=100.0, reference_exit=120.0)

    assert money.gross_pnl == pytest.approx(20.0 * 75)
    assert money.execution_friction == pytest.approx(3.0 * 75)
    assert (money.gross_pnl - money.execution_friction
            - money.total_fees) == pytest.approx(money.net_pnl)
    # And the same answer from the fills alone.
    assert money.net_pnl == pytest.approx(
        (118.0 - 101.0) * 75 - money.total_fees)


def test_favourable_friction_is_refused_rather_than_silently_booked():
    """Friction is the cost of crossing. A fill *better* than its reference
    makes `gross - friction - fees` a different number from what the fills
    say, and reporting either without noticing would be an unreconciled
    ledger."""
    with pytest.raises(ValueError, match="does not reconcile"):
        execution.account(
            entry_side="BUY", entry_price=99.0, exit_price=120.0,
            quantity=75, costs=FlatCostModel(per_round_trip=0.0),
            reference_entry=100.0, reference_exit=120.0)


def test_a_round_trip_cannot_open_and_close_on_the_same_side():
    with pytest.raises(ValueError):
        execution.account(entry_side="BUY", entry_price=100.0,
                          exit_price=110.0, quantity=1, costs=CostModel(),
                          exit_side="BUY")


def test_the_cost_schedule_says_whether_anyone_verified_it():
    """An unverified rate quoted without saying so travels further than the
    caveat attached to it."""
    itemised = describe(CostModel(), SlippageModel())["costs"]
    assert itemised["cost_schedule_status"] == "assumed"
    assert itemised["turnover_basis"] == "option_premium"
    # The rates themselves survive, so an old result stays reproducible
    # after the schedule changes.
    assert itemised["stt_pct_sell"] == 0.10
    assert itemised["rates_as_of"] == "2024-10"

    flat = describe(FlatCostModel(), SlippageModel())["costs"]
    assert flat["turnover_basis"] == "flat_per_round_trip"
    assert flat["cost_schedule_status"] == "configured"


# ---------------------------------------------------------------------------
# SE-2: the gapped entry
# ---------------------------------------------------------------------------

def test_the_default_policy_keeps_the_planned_levels():
    plan = execution.plan_entry(
        "BUY", planned_entry=100.0, planned_stop=99.0, planned_target=102.0,
        actual_entry=100.5)

    assert plan.accepted
    assert plan.stop == 99.0 and plan.target == 102.0
    assert plan.gap_amount == 0.5
    assert plan.risk_per_unit == pytest.approx(1.5)
    assert plan.reward_per_unit == pytest.approx(1.5)
    assert plan.execution_policy == "keep_planned_levels"


def test_shifting_with_the_fill_is_a_named_alternative():
    plan = execution.plan_entry(
        "BUY", planned_entry=100.0, planned_stop=99.0, planned_target=102.0,
        actual_entry=100.5,
        policy=ExecutionPolicy(gapped_entry="shift_levels_with_fill"))

    assert plan.stop == 99.5 and plan.target == 102.5
    assert plan.risk_per_unit == pytest.approx(1.0)
    assert plan.execution_policy == "shift_levels_with_fill"


def test_a_fill_through_the_stop_is_rejected_not_resized():
    plan = execution.plan_entry(
        "BUY", planned_entry=100.0, planned_stop=99.0, planned_target=102.0,
        actual_entry=98.5)

    assert not plan.accepted
    assert plan.rejection == "entry_rejected_due_to_gap"
    assert "98.50" in plan.rejection_detail


def test_a_fill_past_the_target_is_rejected_too():
    """There is nothing left to win, and a trade sized off the sliver of
    remaining reward is a position taken for arithmetic reasons."""
    plan = execution.plan_entry(
        "BUY", planned_entry=100.0, planned_stop=99.0, planned_target=102.0,
        actual_entry=102.5)

    assert not plan.accepted
    assert plan.rejection == "entry_rejected_due_to_gap"


def test_a_short_fill_through_its_stop_is_rejected():
    plan = execution.plan_entry(
        "SELL", planned_entry=100.0, planned_stop=101.0, planned_target=98.0,
        actual_entry=101.5)
    assert not plan.accepted


def test_a_minimum_reward_to_risk_can_be_required():
    """Zero by default — only the degenerate cases are refused — but a run
    may demand more, and the demand is stated in the policy rather than
    buried in the engine."""
    strict = ExecutionPolicy(min_reward_risk=2.0)
    plan = execution.plan_entry(
        "BUY", planned_entry=100.0, planned_stop=99.0, planned_target=102.0,
        actual_entry=100.5, policy=strict)

    assert plan.reward_risk == pytest.approx(1.0)
    assert not plan.accepted
    assert "reward:risk" in plan.rejection_detail


# ---------------------------------------------------------------------------
# PL-3: the gap through the stop
# ---------------------------------------------------------------------------

def test_a_long_stop_that_gapped_fills_at_the_open_not_the_level():
    """Stop at 99, the bar opens at 97. 99 never traded."""
    hit = execution.resolve_levels(
        "BUY", bar_open=97.0, high=97.5, low=96.5, stop=99.0, target=102.0)

    assert hit.price == 97.0
    assert hit.reason == "stop_gap"
    assert hit.gapped is True
    assert hit.level == 99.0


def test_a_short_stop_that_gapped_fills_at_the_open():
    hit = execution.resolve_levels(
        "SELL", bar_open=103.0, high=103.5, low=102.5, stop=101.0, target=98.0)

    assert hit.price == 103.0
    assert hit.reason == "stop_gap"


def test_a_stop_reached_inside_the_bar_fills_at_the_level():
    hit = execution.resolve_levels(
        "BUY", bar_open=100.0, high=100.2, low=98.5, stop=99.0, target=102.0)

    assert hit.price == 99.0
    assert hit.reason == "stop"
    assert hit.gapped is False


def test_the_engine_and_the_evaluator_price_the_same_gap_identically():
    """PL-3 in one line: this is the disagreement that made the dashboard
    show fills at prices the market never offered."""
    for side, open_, stop in (("BUY", 97.0, 99.0), ("SELL", 103.0, 101.0)):
        hit = execution.resolve_levels(
            side, bar_open=open_, high=max(open_, open_) + 0.5,
            low=min(open_, open_) - 0.5, stop=stop,
            target=102.0 if side == "BUY" else 98.0)
        assert hit.price == execution.stop_fill_price(side, stop, open_)


# ---------------------------------------------------------------------------
# PL-4: both levels in one bar
# ---------------------------------------------------------------------------

def test_one_bar_covering_both_levels_takes_the_stop_and_says_it_guessed():
    hit = execution.resolve_levels(
        "BUY", bar_open=100.0, high=102.5, low=98.5, stop=99.0, target=102.0)

    assert hit.reason == "stop"
    assert hit.price == 99.0
    assert hit.ambiguous_intrabar is True


def test_the_ambiguity_policy_is_configurable_and_still_flagged():
    hit = execution.resolve_levels(
        "BUY", bar_open=100.0, high=102.5, low=98.5, stop=99.0, target=102.0,
        policy=ExecutionPolicy(intrabar="target_first"))

    assert hit.reason == "target"
    assert hit.ambiguous_intrabar is True, (
        "a favourable guess is still a guess and must be counted as one")


def test_an_open_already_through_the_stop_settles_the_order():
    """Higher-resolution evidence: the open is the bar's first price, so a
    bar that opens below the stop reached it before anything else."""
    hit = execution.resolve_levels(
        "BUY", bar_open=97.0, high=103.0, low=96.0, stop=99.0, target=102.0)

    assert hit.reason == "stop_gap"
    assert hit.price == 97.0
    assert hit.ambiguous_intrabar is False


def test_an_open_already_through_the_target_settles_it_the_other_way():
    hit = execution.resolve_levels(
        "BUY", bar_open=103.0, high=103.5, low=98.0, stop=99.0, target=102.0)

    assert hit.reason == "target_gap"
    assert hit.price == 103.0
    assert hit.ambiguous_intrabar is False


def test_a_bar_touching_neither_level_closes_nothing():
    assert execution.resolve_levels(
        "BUY", bar_open=100.0, high=101.0, low=99.5, stop=99.0,
        target=102.0) is None


# ---------------------------------------------------------------------------
# FL-4: latency
# ---------------------------------------------------------------------------

def stamps_at(*minutes):
    base = datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=IST)
    return pd.Series([(base + timedelta(minutes=m)).astimezone(UTC)
                      for m in minutes])


def test_zero_latency_fills_on_the_next_bar():
    stamps = stamps_at(0, 5, 10, 15)
    signal_time = stamps.iloc[1]        # the bar 0 close is the bar 1 open
    assert execution.first_executable_index(
        stamps, 0, signal_time, ExecutionPolicy(latency_seconds=0)) == 1


def test_one_second_of_latency_pushes_the_fill_past_that_bar():
    """The bar opening exactly at the decision is no longer reachable: the
    order does not exist yet when that price prints."""
    stamps = stamps_at(0, 5, 10, 15)
    signal_time = stamps.iloc[1]
    assert execution.first_executable_index(
        stamps, 0, signal_time, ExecutionPolicy(latency_seconds=1)) == 2


def test_the_instant_before_a_bar_opens_still_reaches_it():
    """Boundary, from below. 299 seconds after the 09:20 decision is
    09:24:59, and the 09:25 bar opens one second later."""
    stamps = stamps_at(0, 5, 10, 15)
    signal_time = stamps.iloc[1]
    assert execution.first_executable_index(
        stamps, 0, signal_time, ExecutionPolicy(latency_seconds=299)) == 2


def test_a_deadline_exactly_at_a_bars_open_reaches_that_bar():
    """Boundary, exactly. At or after, not strictly after — an order working
    at the instant the bar opens can be filled by it."""
    stamps = stamps_at(0, 5, 10, 15)
    signal_time = stamps.iloc[1]
    assert execution.first_executable_index(
        stamps, 0, signal_time, ExecutionPolicy(latency_seconds=300)) == 2


def test_a_deadline_one_second_after_a_bars_open_misses_it():
    """Boundary, from above."""
    stamps = stamps_at(0, 5, 10, 15)
    signal_time = stamps.iloc[1]
    assert execution.first_executable_index(
        stamps, 0, signal_time, ExecutionPolicy(latency_seconds=301)) == 3


def test_a_latency_past_the_end_of_the_data_has_no_fill():
    stamps = stamps_at(0, 5, 10, 15)
    assert execution.first_executable_index(
        stamps, 0, stamps.iloc[1], ExecutionPolicy(latency_seconds=86_400)) is None


def test_latency_cannot_be_negative():
    with pytest.raises(ValueError):
        ExecutionPolicy(latency_seconds=-1)


def test_the_earliest_execution_time_is_the_signal_plus_the_latency():
    moment = pd.Timestamp("2026-06-17T09:20:00+05:30")
    assert execution.earliest_execution_time(
        moment, ExecutionPolicy(latency_seconds=90)) == moment + pd.Timedelta(seconds=90)


# ---------------------------------------------------------------------------
# the engine, end to end
# ---------------------------------------------------------------------------

def session(n, price=100.0):
    stamps = pd.date_range("2026-06-01 09:15", periods=n, freq="5min",
                           tz="Asia/Kolkata").tz_convert("UTC")
    return pd.DataFrame(dict(timestamp=stamps, open=price, high=price + 0.05,
                             low=price - 0.05, close=price, volume=100.0))


def buy_at(bars_, entry=100.0, stop=99.0, target=102.0):
    def fn(frame):
        action = "BUY" if len(frame) - 1 in bars_ else "HOLD"
        return signal_engine.Signal(
            "NIFTY", "5m", frame.timestamp.iloc[-1].isoformat(), action, 0.8,
            100.0, entry=entry, stop_loss=stop if action == "BUY" else None,
            target=target)
    return fn


def run_engine(frame, signal_fn, **kw):
    return engine.run(frame, signal_fn=signal_fn, warmup=60,
                      risk_config=RiskConfig(capital=100_000, lot_size=1),
                      slippage_pct=0, cost_per_round_trip=0, **kw)


def test_the_engine_records_the_plan_and_the_fill_separately():
    frame = session(75)
    frame.loc[62, ["open", "high", "low", "close"]] = [101.5, 101.6, 101.4, 101.5]
    result = run_engine(frame, buy_at({61}))

    trade = result.trades[0]
    assert trade.planned_entry == 100.0
    assert trade.actual_entry == 101.5
    assert trade.gap_amount == 1.5
    assert trade.planned_stop == 99.0 and trade.stop_loss == 99.0
    assert trade.planned_target == 102.0 and trade.target == 102.0
    assert trade.execution_policy == "keep_planned_levels"
    assert trade.entry_side == "BUY" and trade.exit_side == "SELL"


def test_the_engine_refuses_an_entry_the_gap_invalidated():
    frame = session(75)
    frame.loc[62, ["open", "high", "low", "close"]] = [98.0, 98.1, 97.9, 98.0]
    result = run_engine(frame, buy_at({61}))

    assert result.assumptions["entries_rejected_due_to_gap"] == 1
    # Refused, not merely resized: it appears nowhere as a fill, and the
    # ledger opened no position for it.
    assert result.trades == []
    assert result.dataset["positions"]["entries"] == 0


def test_the_engine_prices_a_gapped_stop_at_the_open():
    frame = session(75)
    frame.loc[63, ["open", "high", "low", "close"]] = [97.0, 97.5, 96.5, 97.0]
    result = run_engine(frame, buy_at({61}))

    trade = result.trades[0]
    assert trade.exit_reason == "stop_gap"
    assert trade.exit == 97.0
    assert result.stats["gapped_exit_count"] == 1


def test_the_engine_counts_the_bars_it_had_to_guess_about():
    frame = session(75)
    frame.loc[63, ["open", "high", "low", "close"]] = [100.0, 102.5, 98.5, 100.0]
    result = run_engine(frame, buy_at({61}))

    trade = result.trades[0]
    assert trade.ambiguous_intrabar is True
    assert trade.exit_reason == "stop"
    assert result.stats["ambiguous_trade_count"] == 1


def test_the_engine_states_its_execution_assumptions():
    result = run_engine(session(75), buy_at({61}))
    stated = result.assumptions["execution"]

    assert stated["gapped_entry_policy"] == "keep_planned_levels"
    assert stated["intrabar_ambiguity_policy"] == "stop_first"
    assert stated["execution_latency_seconds"] == 0.0


def test_latency_moves_the_fill_and_the_timestamps_stay_ordered():
    frame = session(75)
    result = run_engine(frame, buy_at({61}),
                        execution_policy=ExecutionPolicy(latency_seconds=60))

    trade = result.trades[0]
    timing = trade.timing
    signal_time = pd.Timestamp(timing["signal_time"])
    earliest = pd.Timestamp(timing["earliest_execution_time"])
    filled = pd.Timestamp(timing["actual_fill_time"])

    assert earliest == signal_time + pd.Timedelta(seconds=60)
    assert filled >= earliest
    # One bar later than the no-latency fill, because the bar opening at the
    # decision instant is no longer reachable.
    assert filled == frame.timestamp.iloc[63]


# ---------------------------------------------------------------------------
# the engine and the evaluator, reconciled
# ---------------------------------------------------------------------------

class FakeSignal:
    """The two fields `evaluate_signal` reads off a stored row."""
    def __init__(self, action, entry, stop, target):
        self.id = 1
        self.action = action
        self.entry = entry
        self.stop_loss = stop
        self.target = target
        self.confidence = 0.8
        self.context = {"trend": "bullish"}
        self.created_at = None


@pytest.mark.parametrize("scenario,bar,expected_reason,expected_exit", [
    ("gapped stop", [97.0, 97.5, 96.5, 97.0], "stop", 97.0),
    ("stop at the level", [100.0, 100.2, 98.5, 99.5], "stop", 99.0),
    ("both levels in one bar", [100.0, 102.5, 98.5, 100.0], "stop", 99.0),
    ("target", [100.0, 102.5, 99.5, 102.0], "target", 102.0),
])
def test_the_evaluator_prices_an_exit_exactly_as_the_engine_does(
        scenario, bar, expected_reason, expected_exit):
    """Section 8's whole point. These four cases used to give two answers:
    the engine took the bar's open on a gap, the evaluator took the level.
    """
    from app.evaluation import outcomes as study

    frame = session(66)
    frame.loc[63, ["open", "high", "low", "close"]] = bar

    engine_result = run_engine(frame, buy_at({61}))
    assert engine_result.trades, scenario
    traded = engine_result.trades[0]

    replayed = study.evaluate_signal(
        HistoricalFeed(frame), 61,
        FakeSignal("BUY", 100.0, 99.0, 102.0),
        FlatCostModel(per_round_trip=0.0),
        SlippageModel(index_pct=0.0, ticks=0.0), quantity=traded.quantity)

    assert replayed.outcome == expected_reason, scenario
    assert replayed.exit_price == pytest.approx(expected_exit), scenario
    assert replayed.exit_price == pytest.approx(traded.exit), scenario
    assert replayed.stop == traded.stop_loss
    assert replayed.target == traded.target
    assert replayed.ambiguous_intrabar == traded.ambiguous_intrabar, scenario


# ---------------------------------------------------------------------------
# ledger invariants
# ---------------------------------------------------------------------------

def test_every_closed_trade_reconciles_and_keeps_its_sides():
    frame = session(75)
    frame.loc[63, ["low"]] = 98.5
    result = engine.run(frame, signal_fn=buy_at({61}), warmup=60,
                        risk_config=RiskConfig(capital=100_000, lot_size=1),
                        slippage_pct=0.02, cost_model=CostModel())

    assert result.trades
    for trade in result.trades:
        assert trade.entry_side == "BUY" and trade.exit_side == "SELL"
        assert trade.quantity > 0
        assert (trade.gross_pnl - trade.execution_friction
                - trade.fees) == pytest.approx(trade.net_pnl, abs=0.02)
        assert trade.net_pnl == pytest.approx(trade.pnl, abs=0.01)
        assert trade.brokerage + trade.statutory_fees == pytest.approx(
            trade.fees, abs=0.01)
        assert pd.Timestamp(trade.entry_time) < pd.Timestamp(trade.exit_time)
        timing = trade.timing
        assert (pd.Timestamp(timing["actual_fill_time"])
                >= pd.Timestamp(timing["earliest_execution_time"])
                >= pd.Timestamp(timing["signal_time"]))


def test_the_ledger_holds_one_entry_and_one_exit_per_trade():
    frame = session(75)
    frame.loc[63, ["low"]] = 98.5
    result = run_engine(frame, buy_at({61}))
    positions = result.dataset["positions"]

    assert positions["entries"] == len(result.trades)
    assert positions["closed_positions"] == len(result.trades)
    assert positions["state"] in ("CLOSED", "FLAT")


# ---------------------------------------------------------------------------
# FL-5: slippage sensitivity
# ---------------------------------------------------------------------------

def test_the_sweep_scales_assumptions_and_leaves_measurements_alone():
    """Rewritten in 2B.1. It used to assert that `spread_fraction` was
    scaled and clipped at 1.0, which read as a stress on quoted execution
    and was nothing of the kind: the quoted fill path never consults that
    field, so scaling it changed the model object and not one fill. A sweep
    over a real book therefore printed three identical rows and offered
    them as evidence that execution did not matter.

    The line now drawn: assumptions scale, measurements do not.
    """
    base = SlippageModel(ticks=2.0, impact_ticks=1.0, index_pct=0.02,
                         estimated_spread_pct=1.0, spread_fraction=0.5)

    doubled = sensitivity.scale(base, 2.0)
    assert doubled.ticks == 4.0
    assert doubled.impact_ticks == 2.0
    assert doubled.index_pct == 0.04
    assert doubled.estimated_spread_pct == 2.0
    # A quoted spread is the market's number, not this platform's. It is
    # untouched at every multiplier, which is also what stops the spread
    # being charged once as a measurement and again as an assumption.
    assert doubled.spread_fraction == base.spread_fraction

    free = sensitivity.scale(base, 0.0)
    assert free.ticks == 0.0 and free.index_pct == 0.0
    assert free.spread_fraction == base.spread_fraction


def test_a_quoted_book_gets_worse_fills_as_the_impact_assumption_doubles():
    """bid 99 / ask 101, a configured impact, and 0x / 1x / 2x.

    The bug: friction stayed at ₹1 a leg across the whole sweep, because
    the multiplier reached only `spread_fraction`, which the quoted fill
    path ignores. Friction must now worsen monotonically — and the quoted
    spread must stay exactly 2 throughout, or the sweep would be charging
    the spread twice.
    """
    base = SlippageModel(impact_ticks=2.0, tick_size=0.05)   # 0.10 at 1x
    bid, ask = 99.0, 101.0

    buys, sells = [], []
    for multiplier in (0.0, 1.0, 2.0):
        model = sensitivity.scale(base, multiplier)
        bought = costs_module.buy_fill(100.0, model, bid=bid, ask=ask)
        sold = costs_module.sell_fill(100.0, model, bid=bid, ask=ask)

        # The book itself never moves. Only the assumption on top of it does.
        assert ask - bid == 2.0
        assert bought.spread_cost == pytest.approx(1.0)
        assert sold.spread_cost == pytest.approx(1.0)
        assert bought.impact_cost == pytest.approx(0.10 * multiplier)
        assert sold.impact_cost == pytest.approx(0.10 * multiplier)
        # Never better than the touch, at any multiplier.
        assert bought.filled >= ask
        assert sold.filled <= bid
        assert bought.slippage == pytest.approx(
            bought.spread_cost + bought.impact_cost)

        buys.append(bought.slippage)
        sells.append(sold.slippage)

    assert buys[0] < buys[1] < buys[2]
    assert sells[0] < sells[1] < sells[2]
    # 0x does not erase the spread: a zero slippage *assumption* is not a
    # claim that the book was one tick wide.
    assert buys[0] == pytest.approx(1.0)


def test_a_sweep_that_cannot_move_a_quoted_fill_says_so():
    """Zero configured impact is the default, and then the quoted columns
    are identical because there was no assumption left to vary. That is an
    absence of a finding, not a finding, and the flag is what keeps the two
    apart."""
    assert sensitivity.moves_quoted_fills(SlippageModel()) is False
    assert sensitivity.moves_quoted_fills(SlippageModel(impact_ticks=2.0)) is True

    flat = SlippageModel(impact_ticks=0.0)
    for multiplier in (0.0, 1.0, 2.0):
        fill = costs_module.buy_fill(
            100.0, sensitivity.scale(flat, multiplier), bid=99.0, ask=101.0)
        assert fill.filled == pytest.approx(101.0)

    report = sensitivity.sweep(lambda s: type("R", (), {"stats": {"trades": 0}})(),
                               flat)
    assert report["scales_quoted_fills"] is False


def test_the_sweep_reports_each_multiplier_and_flags_a_sign_change():
    frame = session(75)
    frame.loc[63, ["low"]] = 98.5

    def run(slippage):
        return engine.run(frame, signal_fn=buy_at({61}), warmup=60,
                          risk_config=RiskConfig(capital=100_000, lot_size=1),
                          cost_model=FlatCostModel(per_round_trip=0.0),
                          slippage_model=slippage)

    report = sensitivity.sweep(run, SlippageModel(index_pct=0.02))

    assert [row["multiplier"] for row in report["rows"]] == [0.0, 1.0, 2.0]
    for row in report["rows"]:
        assert row["trades"] == 1
        assert row["net_pnl"] is not None
        assert row["max_drawdown_pct"] is not None
    # A losing trade stays losing however cheap the fill, so nothing flips.
    assert report["execution_sensitive"] is False
    assert report["sign_changes"] == []


def test_a_result_that_only_works_at_zero_slippage_is_marked_sensitive():
    """Constructed so the trade wins on a free fill and loses once the
    charges bite. The flag exists precisely so this cannot be quoted at
    whichever multiplier flatters it."""
    class Swings:
        def __init__(self, pnl):
            self.stats = {"trades": 1, "net_pnl": pnl,
                          "expectancy_per_trade": pnl, "expectancy_r": pnl / 100,
                          "profit_factor": None, "max_drawdown_pct": -1.0}

    by_multiplier = {0.0: 500.0, 1.0: 50.0, 2.0: -400.0}
    report = sensitivity.sweep(
        lambda slippage: Swings(by_multiplier[slippage.index_pct / 0.02]),
        SlippageModel(index_pct=0.02))

    assert report["execution_sensitive"] is True
    assert "net_pnl" in report["sign_changes"]
    assert "expectancy_per_trade" in report["sign_changes"]


def test_a_sweep_with_no_trades_says_so_rather_than_reporting_zeroes():
    class Empty:
        stats = {"trades": 0}

    report = sensitivity.sweep(lambda slippage: Empty(), SlippageModel())
    assert all(row["measurable"] is False for row in report["rows"])
    assert report["execution_sensitive"] is False


# ---------------------------------------------------------------------------
# Repair Pass 2B.1 — the execution clock, enforced rather than recorded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("latency,expected_bar", [
    (0, 62),        # the next bar, the standing convention
    (1, 63),        # one second past the decision, so the next bar is gone
    (299, 63),      # still inside the bar that opens at the decision instant
    (300, 63),      # exactly at the next open — eligible, side="left"
    (301, 64),      # one second past it, so the bar after
])
def test_the_evaluator_will_not_fill_before_it_is_allowed_to(latency, expected_bar):
    """The 2B failure, directly. `evaluate_signal` defaulted its fill bar to
    `signal_index + 1` and only `collect` did the latency arithmetic, so a
    direct call filled at the very next open at every latency above and the
    `earliest_execution_time` on the row was a number nothing had checked
    the fill against.

    Bar 61 closes at the open of bar 62, so a latency of 1s already puts
    bar 62 out of reach, 300s lands exactly on bar 63's open and is
    eligible, and 301s misses it.
    """
    from app.evaluation import outcomes as study

    frame = session(70)
    # Distinct opens, so the assertion about *which* bar priced the fill
    # cannot pass by coincidence.
    for bar in (62, 63, 64):
        frame.loc[bar, ["open", "high", "low", "close"]] = [
            100.0 + bar / 100, 100.9, 99.5, 100.0]

    out = study.evaluate_signal(
        HistoricalFeed(frame), 61,
        FakeSignal("BUY", 100.0, 99.0, 102.0),
        FlatCostModel(per_round_trip=0.0),
        SlippageModel(index_pct=0.0, ticks=0.0),
        policy=ExecutionPolicy(latency_seconds=latency))

    earliest = pd.Timestamp(out.earliest_execution_time)
    filled = pd.Timestamp(out.actual_fill_time)

    assert earliest == pd.Timestamp(out.signal_bar_close_time) + pd.Timedelta(
        seconds=latency)
    assert filled >= earliest
    # The fill came from the eligible bar, not from the decision-time one.
    assert filled == frame.timestamp.iloc[expected_bar]
    assert out.entry == pytest.approx(frame.open.iloc[expected_bar])
    assert out.entry != pytest.approx(frame.open.iloc[62]) or latency == 0


def test_the_evaluator_refuses_an_execution_bar_that_precedes_eligibility():
    """A caller that hands over an ineligible bar is refused, rather than
    producing a row whose own timestamps contradict each other."""
    from app.evaluation import outcomes as study

    with pytest.raises(ValueError, match="precedes the earliest"):
        study.evaluate_signal(
            HistoricalFeed(session(70)), 61,
            FakeSignal("BUY", 100.0, 99.0, 102.0),
            FlatCostModel(per_round_trip=0.0), SlippageModel(index_pct=0.0),
            execution_index=62,
            policy=ExecutionPolicy(latency_seconds=600))


def test_a_latency_past_the_archive_is_no_trade_rather_than_the_next_bar():
    from app.evaluation import outcomes as study

    with pytest.raises(study.NoExecutableBar):
        study.evaluate_signal(
            HistoricalFeed(session(64)), 61,
            FakeSignal("BUY", 100.0, 99.0, 102.0),
            FlatCostModel(per_round_trip=0.0), SlippageModel(index_pct=0.0),
            policy=ExecutionPolicy(latency_seconds=86_400))


def test_a_recorded_decision_stamp_starts_the_clock_when_it_is_later():
    """The bar close is a floor on the execution clock, not the clock. A row
    written thirty seconds after its bar closed cannot be filled as though
    the order had been working since the close."""
    from app.evaluation import outcomes as study

    frame = session(70)
    decision = frame.timestamp.iloc[61] + pd.Timedelta(minutes=5, seconds=30)

    out = study.evaluate_signal(
        HistoricalFeed(frame), 61,
        FakeSignal("BUY", 100.0, 99.0, 102.0),
        FlatCostModel(per_round_trip=0.0), SlippageModel(index_pct=0.0),
        decision_time=decision)

    assert pd.Timestamp(out.earliest_execution_time) == decision
    assert pd.Timestamp(out.actual_fill_time) >= decision
    assert pd.Timestamp(out.actual_fill_time) == frame.timestamp.iloc[63]


def test_a_decision_stamp_cannot_move_the_clock_earlier_than_its_own_bar():
    from app.evaluation import outcomes as study

    frame = session(70)
    out = study.evaluate_signal(
        HistoricalFeed(frame), 61,
        FakeSignal("BUY", 100.0, 99.0, 102.0),
        FlatCostModel(per_round_trip=0.0), SlippageModel(index_pct=0.0),
        decision_time=frame.timestamp.iloc[61])       # before the bar closed

    assert pd.Timestamp(out.earliest_execution_time) == pd.Timestamp(
        out.signal_bar_close_time)


# ---------------------------------------------------------------------------
# 2B.1 — the drawdown that is not a portfolio drawdown
# ---------------------------------------------------------------------------

def test_the_overlapping_outcome_curve_is_not_called_a_portfolio_drawdown():
    """It is arithmetically fine and conventionally meaningless: overlapping
    hypothetical outcomes on an unconstrained account that is allowed to go
    into deficit and keep trading. Past -100% an account is empty, so the
    figure cannot be read as an account drawdown — and clipping it to -100%
    would put a portfolio-shaped number on something that is not one."""
    ruin = sensitivity.hypothetical_outcome_curve(
        [-200_000.0, -200_000.0, -100_000.0], starting_capital=100_000.0)

    assert sensitivity.HYPOTHETICAL_DRAWDOWN in ruin
    assert "max_drawdown_pct" not in ruin
    assert ruin[sensitivity.HYPOTHETICAL_DRAWDOWN] < -100.0      # not clipped

    block = ruin["hypothetical_outcome_curve"]
    assert block["equity_went_negative"] is True
    assert block["min_equity"] == -400_000.0
    assert block["starting_capital"] == 100_000.0
    disclosed = " ".join(block["assumptions"]).lower()
    for required in ("overlap", "no capital constraint", "negative",
                     "not an executable portfolio", "not an account-level"):
        assert required in disclosed


def test_a_sequential_engine_curve_keeps_its_own_drawdown_and_says_what_it_is():
    """The engines' curve is a different object — one position at a time,
    marked at exit — so it keeps `max_drawdown_pct`. What it gains is a
    line saying what that is measured on, because it is still not a daily
    mark-to-market portfolio."""
    frame = session(75)
    frame.loc[63, ["low"]] = 98.5
    stats = run_engine(frame, buy_at({61})).stats

    assert "max_drawdown_pct" in stats
    assert sensitivity.HYPOTHETICAL_DRAWDOWN not in stats
    basis = stats["drawdown_basis"]
    assert "non-overlapping" in basis
    assert "not daily mark-to-market" in basis
    assert "no capital constraint" in basis


def test_a_gapped_target_is_described_as_better_not_worse():
    """The 2B caveat said every gapped exit was worse than its level. A
    target the bar gapped through fills better, and the code always did
    that — only the prose was wrong."""
    from app.evaluation import outcomes as study

    better = execution.resolve_levels(
        "BUY", bar_open=103.0, high=103.2, low=102.8, stop=99.0, target=102.0)
    assert better.reason == "target_gap"
    assert better.price == 103.0 > 102.0          # better than the target

    worse = execution.resolve_levels(
        "BUY", bar_open=97.0, high=97.2, low=96.8, stop=99.0, target=102.0)
    assert worse.reason == "stop_gap"
    assert worse.price == 97.0 < 99.0             # worse than the stop

    said = " ".join(study.CAVEATS)
    assert "BETTER than the target" in said
    assert "WORSE than the" in said


# ---------------------------------------------------------------------------
# Repair Pass 2B.2 — refuse before reading, not after
# ---------------------------------------------------------------------------

class CountingFeed(HistoricalFeed):
    """A feed that records every price accessor call.

    The point is sequencing, not the eventual exception. An ineligible
    execution bar that raises *after* its open has been read has already
    committed the look-ahead: the refusal stops a bad number reaching the
    result, it does not stop the price having been consulted. Only a
    counter can tell those two apart.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_reads = 0

    def execution_open(self, index, execution_index):
        self.price_reads += 1
        return super().execution_open(index, execution_index)

    def next_open(self, index):
        self.price_reads += 1
        return super().next_open(index)


def test_an_ineligible_execution_bar_is_refused_before_its_price_is_read():
    """Codex's sequencing finding. The candidate timestamp is compared with
    the execution clock first; the open is read only once it has passed."""
    from app.evaluation import outcomes as study

    feed = CountingFeed(session(70))
    with pytest.raises(ValueError, match="precedes the earliest"):
        study.evaluate_signal(
            feed, 61, FakeSignal("BUY", 100.0, 99.0, 102.0),
            FlatCostModel(per_round_trip=0.0), SlippageModel(index_pct=0.0),
            execution_index=62,
            policy=ExecutionPolicy(latency_seconds=600))

    assert feed.price_reads == 0


def test_an_eligible_execution_bar_does_read_its_price():
    """The counter has to be able to move, or the test above proves only
    that nothing ever reads anything."""
    from app.evaluation import outcomes as study

    feed = CountingFeed(session(70))
    study.evaluate_signal(
        feed, 61, FakeSignal("BUY", 100.0, 99.0, 102.0),
        FlatCostModel(per_round_trip=0.0), SlippageModel(index_pct=0.0))

    assert feed.price_reads >= 1


def test_the_feed_can_give_a_fill_time_without_giving_a_price():
    feed = CountingFeed(session(70))
    feed.seek(61)
    stamp = feed.execution_timestamp(61, 64)

    assert stamp == feed.stamps().iloc[64]
    assert feed.price_reads == 0


@pytest.mark.parametrize("latency,expected_bar", [
    (0, 62), (1, 63), (299, 63), (300, 63), (301, 64),
])
def test_the_latency_matrix_reads_no_bar_before_eligibility(latency, expected_bar):
    """The 2B.1 boundary matrix, re-run with the accessor counted. Every
    case must fill at or after its deadline *and* reach exactly one open —
    the eligible one — with no pre-eligibility read on the way."""
    from app.evaluation import outcomes as study

    frame = session(70)
    for bar in (62, 63, 64):
        frame.loc[bar, ["open", "high", "low", "close"]] = [
            100.0 + bar / 100, 100.9, 99.5, 100.0]

    feed = CountingFeed(frame)
    out = study.evaluate_signal(
        feed, 61, FakeSignal("BUY", 100.0, 99.0, 102.0),
        FlatCostModel(per_round_trip=0.0),
        SlippageModel(index_pct=0.0, ticks=0.0),
        policy=ExecutionPolicy(latency_seconds=latency))

    assert pd.Timestamp(out.actual_fill_time) >= pd.Timestamp(
        out.earliest_execution_time)
    assert pd.Timestamp(out.actual_fill_time) == frame.timestamp.iloc[expected_bar]
    assert out.entry == pytest.approx(frame.open.iloc[expected_bar])
    # Exactly one forward reach for the fill, and it was the eligible bar.
    assert feed.price_reads == 1


# ---------------------------------------------------------------------------
# 2B.2 — drawdown semantics survive the projection layers
# ---------------------------------------------------------------------------

def test_the_hypothetical_drawdown_carries_its_assumptions_through_the_sweep():
    """The metric was renamed correctly in 2B.1 and then projected as a bare
    number: `sweep` copied the percentage and dropped the block that says
    what curve it came from. A figure of -389% with no assumptions beside it
    is ambiguous again, which is the thing the rename was for."""
    curve = sensitivity.hypothetical_outcome_curve(
        [-200_000.0, -200_000.0], starting_capital=100_000.0)

    class Study:
        stats = {"trades": 10, "net_pnl": -400_000.0,
                 "expectancy_per_trade": -40_000.0, "expectancy_r": -2.0,
                 "profit_factor": 0.0, **curve}

    report = sensitivity.sweep(lambda slippage: Study(), SlippageModel())

    for row in report["rows"]:
        assert row[sensitivity.HYPOTHETICAL_DRAWDOWN] < -100.0
        block = row["hypothetical_outcome_curve"]
        assert block["overlapping_outcomes"] is True
        assert block["capital_constrained"] is False
        assert block["equity_can_go_negative"] is True
        assert block["marked_at_resolution"] is True
        assert block["executable_portfolio_curve"] is False
        assert block["starting_capital"] == 100_000.0
        assert block["min_equity"] == -300_000.0
        assert block["equity_went_negative"] is True
        assert block["assumptions"]
        # And it is never relabelled as the sequential metric on the way.
        assert "max_drawdown_pct" not in row


def test_the_sequential_drawdown_basis_survives_the_sweep_and_the_api():
    """The engines' curve means something different and has to keep saying
    so wherever it is projected."""
    from app.api import backtest as api

    frame = session(75)
    frame.loc[63, ["low"]] = 98.5
    result = run_engine(frame, buy_at({61}))

    swept = sensitivity.sweep(lambda slippage: result, SlippageModel())
    for row in swept["rows"]:
        assert row["max_drawdown_pct"] is not None
        assert "non-overlapping" in row["drawdown_basis"]
        assert sensitivity.HYPOTHETICAL_DRAWDOWN not in row

    projected = api.drawdown_fields(result.stats)
    assert projected["max_drawdown_pct"] == result.stats["max_drawdown_pct"]
    for phrase in ("realised_pnl_at_exit", "one position at a time",
                   "non-overlapping", "no capital constraint",
                   "not daily mark-to-market"):
        assert phrase in projected["drawdown_basis"]
