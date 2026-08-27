"""Which contract a direction actually buys, and when to refuse.

An index signal says "up". Most of an option buyer's result lives in the gap
between that and "24,350 CE expiring Tuesday" — the same correct call is a
good trade at one strike and a donation at another.

Every refusal here is a named code rather than a skipped bar, because a
strategy that silently drops the signals it cannot price reports the win
rate of the subset it happened to like.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.market_hours import IST
from app.optionbuy import contracts as contract_module
from app.optionbuy.chain import ChainStore, ContractKey
from app.optionbuy.contracts import SelectionConfig, select
from optionbuy_fixtures import option_bars, sessions

DAYS = sessions(date(2025, 6, 2), 3)          # Mon 2 Jun -> Wed 4 Jun 2025
NEAR = date(2025, 6, 3)                       # Tuesday, the front weekly
FAR = date(2025, 6, 10)                       # the following Tuesday
MONTHLY = date(2025, 6, 24)

# Monday 09:15 IST, decision time. From here the Tuesday weekly is 1.26
# days out — the day *before* expiry, which clears the one-day floor.
MOMENT = datetime(2025, 6, 2, 9, 15, tzinfo=IST)
# Tuesday morning, which is expiry day for the 3 June weekly: 0.26 days.
ON_EXPIRY_DAY = datetime(2025, 6, 3, 9, 15, tzinfo=IST)
SPOT = 24_000.0


def store_with(*expiries, **kwargs) -> ChainStore:
    bars = {}
    for expiry in expiries:
        bars |= option_bars(DAYS, expiry, **kwargs)
    store = ChainStore(bars, {})
    store.seek(MOMENT + timedelta(days=5))
    return store


def choose(store, action="BUY", config=None, moment=MOMENT, spot=SPOT,
           use_archive=True):
    return select(action=action, spot=spot, moment=moment, store=store,
                  config=config or SelectionConfig(), use_archive=use_archive)


# ---- side -------------------------------------------------------------

def test_buy_takes_a_call_and_sell_takes_a_put():
    store = store_with(FAR)
    call, _ = choose(store, "BUY")
    put, _ = choose(store, "SELL")
    assert call.option_type == "CE"
    assert put.option_type == "PE"


def test_a_hold_has_no_contract_to_buy():
    with pytest.raises(ValueError, match="not a directional action"):
        contract_module.side_for("HOLD")


# ---- expiry -----------------------------------------------------------

def test_the_nearest_qualifying_expiry_is_taken():
    store = store_with(FAR, MONTHLY)
    chosen, _ = choose(store)
    assert chosen.key.expiry == FAR


def test_the_day_before_expiry_still_qualifies():
    """The floor excludes expiry day, not the whole final week. Monday's
    decision on a Tuesday weekly has 1.26 days of tenor left."""
    store = store_with(NEAR, FAR)
    chosen, _ = choose(store, moment=MOMENT)
    assert chosen.key.expiry == NEAR
    assert 1.0 < chosen.days_to_expiry < 1.5


def test_expiry_day_itself_is_excluded_by_default():
    """Inside the last day the premium is priced off the clock rather than
    off the signal, and gamma decides the outcome."""
    store = store_with(NEAR, FAR)
    chosen, _ = choose(store, moment=ON_EXPIRY_DAY)
    assert chosen.key.expiry == FAR


def test_an_expiry_inside_the_floor_reports_why_it_was_skipped():
    store = store_with(NEAR)
    chosen, rejected = choose(store, moment=ON_EXPIRY_DAY)
    assert chosen is None
    assert rejected.code == contract_module.EXPIRY_TOO_NEAR
    assert "gamma" in rejected.detail


def test_an_expiry_beyond_the_ceiling_is_refused():
    store = store_with(MONTHLY)
    chosen, rejected = choose(store)
    assert chosen is None
    assert rejected.code == contract_module.EXPIRY_TOO_FAR
    assert "22" in rejected.detail or "23" in rejected.detail


def test_the_next_policy_skips_the_front_week():
    """The way to ask whether a front-week result was a gamma artefact."""
    store = store_with(FAR, MONTHLY)
    config = SelectionConfig(expiry_policy=contract_module.NEXT,
                             max_days_to_expiry=40)
    chosen, _ = choose(store, config=config)
    assert chosen.key.expiry == MONTHLY


def test_only_expiries_the_archive_priced_are_offered():
    """A contract row nobody ever quoted is not something a backtest can buy."""
    store = store_with(FAR)
    chosen, rejected = choose(store)
    assert chosen.key.expiry == FAR

    empty = ChainStore({}, {})
    empty.seek(MOMENT)
    chosen, rejected = choose(empty)
    assert chosen is None
    assert rejected.code == contract_module.NO_EXPIRY
    assert "option archive" in rejected.detail


def test_the_weekly_calendar_is_only_used_without_an_archive():
    empty = ChainStore({}, {})
    empty.seek(MOMENT)
    chosen, _ = choose(empty, use_archive=False)
    assert chosen is not None
    # Tuesday weeklies. Monday's decision reaches Tuesday the 3rd with 1.26
    # days of tenor, which clears the floor.
    assert chosen.key.expiry == date(2025, 6, 3)
    assert "weekly calendar" in " ".join(chosen.reasons)


def test_the_synthetic_calendar_rolls_past_an_expiry_inside_the_floor():
    empty = ChainStore({}, {})
    empty.seek(ON_EXPIRY_DAY)
    chosen, _ = choose(empty, moment=ON_EXPIRY_DAY, use_archive=False)
    assert chosen.key.expiry == date(2025, 6, 10)


def test_a_synthetic_expiry_never_lands_in_the_past():
    for offset in range(0, 14):
        moment = MOMENT + timedelta(days=offset)
        listed = contract_module.synthetic_expiries(moment, weekday=1)
        assert all(contract_module.expiry_moment(d) > moment for d in listed)


# ---- strike -----------------------------------------------------------

def test_at_the_money_is_the_default():
    store = store_with(FAR)
    chosen, _ = choose(store, spot=24_012.0)
    assert chosen.strike == 24_000.0
    assert chosen.moneyness == "ATM"


def test_an_offset_moves_the_strike_the_right_way_for_each_side():
    """Out of the money means *up* for a call and *down* for a put. A single
    signed offset applied to both would buy an ITM put while calling it OTM."""
    store = store_with(FAR)
    config = SelectionConfig(strike_policy=contract_module.OFFSET,
                             strike_offset=2)
    call, _ = choose(store, "BUY", config=config)
    put, _ = choose(store, "SELL", config=config)

    assert call.strike == 24_100.0 and call.moneyness == "OTM"
    assert put.strike == 23_900.0 and put.moneyness == "OTM"


def test_a_computed_strike_snaps_to_the_listed_ladder():
    """NIFTY lists 50-point strikes near the money and 100-point strikes
    further out, so a computed strike is regularly a contract nobody could
    have traded."""
    store = store_with(FAR, strikes=[23_800.0, 24_000.0, 24_200.0])
    config = SelectionConfig(strike_policy=contract_module.OFFSET,
                             strike_offset=1)          # wants 24,050
    chosen, _ = choose(store, config=config)

    assert chosen.strike == 24_000.0
    assert "Wanted 24050" in " ".join(chosen.reasons)


def test_no_listed_strike_is_a_refusal_not_a_guess():
    store = store_with(FAR, types=("PE",))
    chosen, rejected = choose(store, "BUY")
    assert chosen is None
    assert rejected.code == contract_module.STRIKE_NOT_LISTED


def test_delta_targeting_picks_the_closest_listed_delta():
    store = store_with(FAR)
    config = SelectionConfig(strike_policy=contract_module.DELTA,
                             target_delta=0.35)
    chosen, _ = choose(store, config=config)

    assert chosen.delta is not None
    assert 0.20 <= abs(chosen.delta) <= 0.50
    # A 0.35 delta call sits above the money.
    assert chosen.strike > SPOT
    assert "modelled" in " ".join(chosen.reasons)


def test_delta_targeting_is_labelled_modelled_even_on_an_observed_run():
    """The archive stores IV per contract but never delta. Choosing by delta
    is a model choice; only the pricing that follows is observed."""
    store = store_with(FAR)
    config = SelectionConfig(strike_policy=contract_module.DELTA)
    chosen, _ = choose(store, config=config)
    assert any("modelled" in r for r in chosen.reasons)


# ---- liquidity ---------------------------------------------------------

def test_open_interest_below_the_floor_is_rejected():
    store = store_with(FAR, open_interest=1_000.0)
    config = SelectionConfig(min_open_interest=100_000)
    chosen, rejected = choose(store, config=config)
    assert chosen is None
    assert rejected.code == contract_module.THIN_OPEN_INTEREST
    assert "1000" in rejected.detail.replace(",", "")


def test_volume_below_the_floor_is_rejected():
    store = store_with(FAR, volume=10.0)
    config = SelectionConfig(min_volume=1_000)
    chosen, rejected = choose(store, config=config)
    assert chosen is None
    assert rejected.code == contract_module.THIN_VOLUME


def test_a_missing_field_fails_the_filter_that_needs_it():
    """A null is not a pass. NSE's public chain publishes no depth, and
    treating "not stored" as "wide enough" would report a filter as applied
    when it never ran."""
    store = store_with(FAR)
    for key, bars in store._bars.items():                # noqa: SLF001
        store._bars[key] = [                             # noqa: SLF001
            type(b)(**{**b.__dict__, "open_interest": None}) for b in bars]

    chosen, rejected = choose(store, config=SelectionConfig(min_open_interest=1))
    assert chosen is None
    assert rejected.code == contract_module.THIN_OPEN_INTEREST
    assert "stores no open interest" in rejected.detail


def test_a_wide_quoted_spread_is_rejected_when_depth_exists():
    store = store_with(FAR, bid_ask_spread=200.0)
    chosen, rejected = choose(store, config=SelectionConfig(max_spread_pct=5.0))
    assert chosen is None
    assert rejected.code == contract_module.WIDE_SPREAD


def test_a_tight_spread_passes_and_is_reported_as_measured():
    store = store_with(FAR, bid_ask_spread=1.0)
    chosen, _ = choose(store)
    assert chosen.liquidity.checked
    assert chosen.liquidity.spread == pytest.approx(1.0)
    assert "spread" in chosen.liquidity.note


def test_a_listed_strike_with_no_current_quote_is_rejected():
    """The ladder is what was ever listed; the quote has to be current.

    A contract the collector stopped writing at 09:30 is still in the chain
    at 11:00 — it is the *price* that is missing, and filling from the last
    one it happened to catch is how a stale premium becomes a fill.
    """
    late = datetime(2025, 6, 2, 11, 0, tzinfo=IST)
    store = store_with(FAR)
    thin = ContractKey(FAR, 24_000.0, "CE")
    early = datetime(2025, 6, 2, 9, 30, tzinfo=IST)
    store._bars[thin] = [b for b in store._bars[thin]           # noqa: SLF001
                         if b.timestamp <= early]
    store._stamps[thin] = [b.timestamp for b in store._bars[thin]]   # noqa: SLF001

    # Still listed, so selection reaches it rather than snapping past it.
    assert 24_000.0 in store.strikes(FAR, "CE", late)

    chosen, rejected = choose(store, moment=late)
    assert chosen is None
    assert rejected.code == contract_module.NO_QUOTE
    assert "collector had nothing" in rejected.detail


def test_a_premium_below_the_floor_is_rejected():
    store = store_with(FAR, strikes=[24_000.0])
    chosen, rejected = choose(store, spot=SPOT,
                              config=SelectionConfig(min_premium=10_000.0))
    assert chosen is None
    assert rejected.code == contract_module.PREMIUM_TOO_LOW


def test_liquidity_is_reported_unchecked_rather_than_passed_without_an_archive():
    """An unchecked filter reported as a passed one is how a backtest claims
    it avoided illiquid strikes when it never looked at one."""
    empty = ChainStore({}, {})
    empty.seek(MOMENT)
    chosen, _ = choose(empty, use_archive=False)

    assert chosen.liquidity.checked is False
    assert "unchecked" in chosen.liquidity.note


# ---- configuration -----------------------------------------------------

def test_an_impossible_tenor_window_is_rejected_at_configuration_time():
    config = SelectionConfig(min_days_to_expiry=10, max_days_to_expiry=2)
    with pytest.raises(ValueError, match="no expiry can satisfy"):
        config.validate()


def test_an_unknown_policy_is_rejected_rather_than_defaulted():
    with pytest.raises(ValueError, match="unknown strike policy"):
        SelectionConfig(strike_policy="cheapest").validate()
