"""Strategy v2's rules, each tested at the boundary it draws."""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import option_pricing
from app.market_hours import IST
from app.strategy_v2 import rules
from app.strategy_v2.config import V2Config

CFG = V2Config()
NOW = datetime(2026, 9, 17, 10, 30, tzinfo=IST)


def signal(action="BUY", *, bias="BULLISH", state="ENTER_NOW", stop=23950.0,
           target=24100.0, generated_at=None):
    return {"action": action, "stop_loss": stop, "target": target,
            "generated_at": (generated_at or NOW).isoformat(),
            "plan": {"bias": {"label": bias}, "entry": {"state": state}}}


# ---- the signal -----------------------------------------------------------

def test_a_hold_is_not_a_trade():
    assert rules.signal_rejection(signal("HOLD"), now=NOW, cfg=CFG).code == rules.HOLD


def test_a_signal_without_levels_is_refused():
    s = signal()
    s["target"] = None
    assert rules.signal_rejection(s, now=NOW, cfg=CFG).code == rules.NO_LEVELS


def test_a_signal_first_seen_too_long_ago_is_refused():
    old = signal(generated_at=NOW - timedelta(seconds=CFG.max_signal_age_seconds + 1))
    fresh = signal(generated_at=NOW - timedelta(seconds=CFG.max_signal_age_seconds - 1))
    assert rules.signal_rejection(old, now=NOW, cfg=CFG).code == rules.STALE_SIGNAL
    assert rules.signal_rejection(fresh, now=NOW, cfg=CFG) is None


def test_the_bar_timestamp_is_not_mistaken_for_the_signal_age():
    s = signal()
    del s["generated_at"]
    s["timestamp"] = (NOW - timedelta(hours=3)).isoformat()
    assert rules.signal_rejection(s, now=NOW, cfg=CFG) is None


@pytest.mark.parametrize("state", ["WAIT_PULLBACK", "WAIT_BREAKOUT", "NO_ENTRY", None])
def test_only_enter_now_is_accepted(state):
    assert rules.signal_rejection(signal(state=state), now=NOW, cfg=CFG).code == rules.ENTRY_STATE


@pytest.mark.parametrize("action,bias", [("BUY", "BEARISH"), ("BUY", "NEUTRAL"),
                                         ("SELL", "BULLISH"), ("SELL", None)])
def test_the_bias_is_a_hard_veto(action, bias):
    got = rules.signal_rejection(signal(action, bias=bias, stop=24100, target=23900),
                                 now=NOW, cfg=CFG)
    assert got.code == rules.BIAS


def test_an_aligned_signal_passes():
    assert rules.signal_rejection(signal("SELL", bias="BEARISH", stop=24100, target=23900),
                                  now=NOW, cfg=CFG) is None


def test_a_move_already_past_the_stop_or_target_is_refused():
    assert rules.level_rejection("BUY", 23940, 23950, 24100).code == rules.PAST_STOP
    assert rules.level_rejection("BUY", 24100, 23950, 24100).code == rules.PAST_TARGET
    assert rules.level_rejection("SELL", 24100, 24100, 23900).code == rules.PAST_STOP
    assert rules.level_rejection("BUY", 24000, 23950, 24100) is None


# ---- the calendar ----------------------------------------------------------

def test_sessions_skip_weekends_and_exchange_holidays():
    # Mon 14-Sep-2026 is Ganesh Chaturthi. From Friday, Tuesday's expiry has
    # one session left, not two.
    assert rules.sessions_until(date(2026, 9, 11), date(2026, 9, 15)) == 1
    assert rules.sessions_until(date(2026, 9, 17), date(2026, 9, 22)) == 3
    assert rules.sessions_until(date(2026, 9, 15), date(2026, 9, 15)) == 0


EXPIRIES = [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]


@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 11), date(2026, 9, 22)),   # holiday leaves Tue with 1 session
    (date(2026, 9, 15), date(2026, 9, 22)),   # expiry day itself rolls
    (date(2026, 9, 17), date(2026, 9, 22)),   # Fri, Mon, Tue = 3
    (date(2026, 9, 18), date(2026, 9, 22)),   # Mon, Tue = 2, exactly the floor
    (date(2026, 9, 21), date(2026, 9, 29)),   # Tue only = 1, rolls
])
def test_the_nearest_weekly_with_two_sessions_left_is_chosen(today, expected):
    assert rules.choose_expiry(EXPIRIES, today, CFG) == expected


def test_no_expiry_far_enough_is_none():
    assert rules.choose_expiry([date(2026, 9, 15)], date(2026, 9, 15), CFG) is None


def test_expiry_day_is_recognised_from_the_listed_calendar():
    assert rules.is_expiry_day(date(2026, 9, 22), EXPIRIES)
    assert not rules.is_expiry_day(date(2026, 9, 21), EXPIRIES)


@pytest.mark.parametrize("clock,inside", [
    (time(9, 29), False), (time(9, 30), True), (time(14, 30), True), (time(14, 31), False)])
def test_the_entry_window(clock, inside):
    moment = datetime.combine(date(2026, 9, 17), clock, tzinfo=IST)
    assert rules.in_entry_window(moment, CFG) is inside


# ---- India VIX --------------------------------------------------------------

HISTORY = [10.0 + (i % 100) * 0.1 for i in range(252)]     # 10.0 .. 19.9


def test_the_gate_fails_closed_without_enough_history():
    got = rules.vix_gate(HISTORY[:CFG.vix_min_history - 1], 12.0, CFG)
    assert not got.ok and got.code == rules.VIX_NO_HISTORY


def test_the_gate_fails_closed_without_a_live_reading():
    assert rules.vix_gate(HISTORY, None, CFG).code == rules.VIX_NO_LIVE


def test_percentile_rank_counts_ties_as_at_or_below():
    assert rules.percentile_rank([10, 11, 12, 13], 12) == 75.0


def test_expensive_volatility_blocks():
    got = rules.vix_gate(HISTORY, 19.0, CFG)
    assert not got.ok and got.code == rules.VIX_HIGH and got.percentile >= 80


def test_a_spike_on_the_day_blocks_even_when_the_level_is_ordinary():
    history = HISTORY[:-1] + [11.0]
    got = rules.vix_gate(history, 12.2, CFG)                # +10.9% on 11.0
    assert not got.ok and got.code == rules.VIX_SPIKE


def test_calm_volatility_passes_and_reports_its_numbers():
    history = HISTORY[:-1] + [12.0]
    got = rules.vix_gate(history, 12.3, CFG)
    assert got.ok, got.detail
    assert got.previous_close == 12.0 and got.spike_pct == pytest.approx(2.5)


# ---- the contract ---------------------------------------------------------------

SPOT = 24000.0
YEARS = 4 / 252
IV = 0.13


def chain(option_type="CE", *, age=1.0, spread=0.5, skip=()):
    out = []
    for strike in range(23500, 24550, 50):
        if strike in skip:
            continue
        fair = option_pricing.price(SPOT, strike, YEARS, IV, kind=option_type)
        if fair < 1:
            continue
        out.append(rules.Candidate(strike=float(strike), option_type=option_type,
                                   ltp=round(fair, 2), bid=round(fair - spread / 2, 2),
                                   ask=round(fair + spread / 2, 2), age_seconds=age,
                                   token=str(strike), lot_size=65))
    return out


def test_the_strike_nearest_half_delta_is_bought():
    pick, rejected = rules.pick_contract(chain(), option_type="CE", spot=SPOT,
                                         years=YEARS, cfg=CFG)
    assert rejected is None
    # Carry pushes the at-the-money call's delta to ~0.53, so the strike one
    # step up can be the nearer 0.50. The rule is "nearest delta", not "ATM".
    def delta(strike):
        return abs(option_pricing.greeks(SPOT, strike, YEARS, IV).delta)
    nearest = min(range(23500, 24550, 50), key=lambda k: abs(delta(k) - 0.5))
    assert pick.candidate.strike == nearest
    assert abs(pick.candidate.strike - SPOT) <= 50
    assert CFG.min_delta <= pick.delta <= CFG.max_delta
    assert pick.iv == pytest.approx(IV, abs=0.01)


def test_puts_are_picked_from_puts():
    pick, _ = rules.pick_contract(chain("PE") + chain("CE"), option_type="PE",
                                  spot=SPOT, years=YEARS, cfg=CFG)
    assert pick.candidate.option_type == "PE"


def test_a_stale_best_strike_gives_way_to_the_next_liquid_one_in_band():
    fresh = chain()
    stale_atm = [c for c in fresh if c.strike == 24000.0][0]
    stale_atm.age_seconds = CFG.max_quote_age_seconds + 5
    pick, rejected = rules.pick_contract(fresh, option_type="CE", spot=SPOT,
                                         years=YEARS, cfg=CFG)
    assert rejected is None and pick.candidate.strike != 24000.0
    assert CFG.min_delta <= pick.delta <= CFG.max_delta


def test_when_nothing_in_band_is_liquid_the_reason_is_named():
    _, rejected = rules.pick_contract(chain(spread=40.0), option_type="CE",
                                      spot=SPOT, years=YEARS, cfg=CFG)
    assert rejected.code == rules.WIDE_SPREAD


def test_a_chain_with_no_strike_in_the_delta_band_is_refused():
    far = chain(skip=range(23700, 24350, 50))
    _, rejected = rules.pick_contract(far, option_type="CE", spot=SPOT,
                                      years=YEARS, cfg=CFG)
    assert rejected.code == rules.NO_DELTA


def test_an_empty_chain_is_not_ready():
    _, rejected = rules.pick_contract([], option_type="CE", spot=SPOT, years=YEARS, cfg=CFG)
    assert rejected.code == rules.CHAIN_NOT_READY


def test_one_sided_quotes_are_not_bought():
    one_sided = chain()
    for c in one_sided:
        c.bid = None
    _, rejected = rules.pick_contract(one_sided, option_type="CE", spot=SPOT,
                                      years=YEARS, cfg=CFG)
    assert rejected.code == rules.NO_DEPTH


# ---- the levels ---------------------------------------------------------------

def test_a_near_index_stop_sets_the_premium_stop():
    entry = option_pricing.price(SPOT, 24000, YEARS, IV)
    got = rules.premium_levels(entry=entry, strike=24000, option_type="CE", years=YEARS,
                               iv=IV, index_stop=23960, index_target=24080, cfg=CFG)
    assert got.stop_basis == "index_stop"
    assert entry * 0.70 < got.stop < entry
    assert got.target_basis == "index_target" and got.target > entry


def test_a_distant_stop_is_capped_at_thirty_percent_and_a_distant_target_at_fifty():
    entry = option_pricing.price(SPOT, 24000, YEARS, IV)
    got = rules.premium_levels(entry=entry, strike=24000, option_type="CE", years=YEARS,
                               iv=IV, index_stop=23500, index_target=24600, cfg=CFG)
    assert got.stop == pytest.approx(entry * 0.70)
    assert got.target == pytest.approx(entry * 1.50)
    assert (got.stop_basis, got.target_basis) == ("premium_pct", "premium_pct")


def test_put_levels_move_the_right_way():
    entry = option_pricing.price(SPOT, 24000, YEARS, IV, kind="PE")
    got = rules.premium_levels(entry=entry, strike=24000, option_type="PE", years=YEARS,
                               iv=IV, index_stop=24040, index_target=23920, cfg=CFG)
    assert got.stop < entry < got.target


# ---- the exit ------------------------------------------------------------------

OPENED = datetime(2026, 9, 17, 10, 0, tzinfo=IST)


def exit_at(*, bid=100.0, spot=24000.0, now=OPENED + timedelta(minutes=5), direction="BUY"):
    if direction == "BUY":
        levels = dict(index_stop=23950.0, index_target=24100.0)
    else:
        levels = dict(index_stop=24050.0, index_target=23900.0)
    return rules.exit_reason(direction=direction, bid=bid, spot=spot,
                             premium_stop=80.0, premium_target=140.0,
                             now_ist=now, opened_at_ist=OPENED, cfg=CFG, **levels)


def test_nothing_fires_inside_the_levels():
    assert exit_at() is None


def test_losses_are_checked_before_gains():
    assert exit_at(bid=79.0, spot=24150.0) == rules.EXIT_PREMIUM_STOP
    assert exit_at(bid=150.0, spot=23940.0) == rules.EXIT_INDEX_STOP


def test_each_target_closes():
    assert exit_at(bid=140.0) == rules.EXIT_PREMIUM_TARGET
    assert exit_at(spot=24100.0) == rules.EXIT_INDEX_TARGET


def test_short_index_levels_are_mirrored():
    assert exit_at(direction="SELL", spot=24050.0) == rules.EXIT_INDEX_STOP
    assert exit_at(direction="SELL", spot=23900.0) == rules.EXIT_INDEX_TARGET


def test_the_session_and_the_clock_close_a_quiet_position():
    assert exit_at(now=datetime(2026, 9, 17, 15, 15, tzinfo=IST)) == rules.EXIT_SESSION_END
    assert exit_at(now=OPENED + timedelta(minutes=CFG.max_minutes_in_trade)) == rules.EXIT_TIME


def test_a_missing_quote_does_not_close_on_premium_but_the_index_still_can():
    assert exit_at(bid=None) is None
    assert exit_at(bid=None, spot=23900.0) == rules.EXIT_INDEX_STOP
