"""Strategy v2's paper trader, driven through whole sessions.

No socket, no Redis, no broker: every input is a seam. The chain is a real
`LiveChain` fed option ticks priced by Black-Scholes, so the delta pick,
the levels and the sizing all run on the same code the live desk does.
"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import option_pricing
from app.brokers.angel import OptionTick
from app.data import option_universe as ou
from app.market_hours import IST
from app.models import PaperDecision, PaperPosition, VixDaily
from app.optionbuy.contracts import expiry_moment
from app.strategy_v2 import rules
from app.strategy_v2.config import V2Config
from app.strategy_v2.paper import PaperTrader, fingerprint
from app.workers.option_chain_live import LiveChain
from test_v2_feed import two_expiry_master

THU = datetime(2026, 9, 17, 10, 30, tzinfo=IST)
EXPIRY = date(2026, 9, 22)
IV = 0.13


class Clock:
    def __init__(self, start=THU):
        self.now = start.astimezone(UTC)

    def __call__(self):
        return self.now

    def at(self, hour, minute):
        self.now = self.now.astimezone(IST).replace(hour=hour, minute=minute).astimezone(UTC)

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Desk:
    """Everything the trader reads, held where a test can change it."""

    def __init__(self, engine, clock):
        self.clock = clock
        self.spot = 24000.0
        self.vix = 12.0
        self.signal = None
        self.feed_ok = True
        self.kill = False
        self.listed = [EXPIRY, date(2026, 9, 29)]
        self.market_open = True
        self.published = []
        self.chain = LiveChain(clock=clock)
        self.chain.max_age_seconds = 900
        self.chain.set_universe(ou.build(two_expiry_master(), 24000.0, band=10,
                                         expiry=EXPIRY))
        self.session = lambda: Session(engine, expire_on_commit=False)

    def quote_all(self, spread=0.10, spot=None, lot_size=None):
        spot = spot or self.spot
        moment = self.clock()
        years = option_pricing.years_to_expiry(moment.astimezone(IST), expiry_moment(EXPIRY))
        for c in self.chain.universe.contracts:
            fair = option_pricing.price(spot, c.strike, years, IV, kind=c.option_type)
            self.tick(c.token, fair, spread)

    def tick(self, token, fair, spread=0.10):
        fair = max(fair, 0.10)
        self.chain.update(OptionTick(token=token, price=round(fair, 2),
                                     source_time=self.clock(), open_interest=1000,
                                     volume=500, bid=round(fair - spread / 2, 2),
                                     ask=round(fair + spread / 2, 2)))

    def trader(self, cfg=None):
        return PaperTrader(
            cfg=cfg or V2Config(), session_factory=self.session, clock=self.clock,
            signal_source=lambda: self.signal, spot_source=lambda: self.spot,
            vix_source=lambda: self.vix,
            chain_lookup=lambda e: self.chain if e == EXPIRY else None,
            listed_expiries=lambda: self.listed, feed_healthy=lambda: self.feed_ok,
            kill_switch=lambda: self.kill, publish_fn=self.published.append,
            market_open=lambda now: self.market_open)


def buy_signal(stop=23950.0, target=24110.0, **extra):
    return {"timestamp": "2026-09-17T10:25:00+05:30", "action": "BUY", "price": 24000.0,
            "confidence": 0.62, "entry": 24000.0, "stop_loss": stop, "target": target,
            "plan": {"bias": {"label": "BULLISH"}, "entry": {"state": "ENTER_NOW"}},
            **extra}


@pytest.fixture
def desk(engine):
    clock = Clock()
    d = Desk(engine, clock)
    with d.session() as db:
        start = date(2025, 9, 1)
        for n in range(200):
            db.add(VixDaily(session_date=start + timedelta(days=n),
                            close=10.0 + (n % 60) * 0.1, source="test"))
        db.commit()
    d.quote_all()
    return d


def decisions(desk):
    with desk.session() as db:
        return [(d.outcome, d.code) for d in db.query(PaperDecision).order_by(PaperDecision.id)]


def positions(desk):
    with desk.session() as db:
        return db.query(PaperPosition).order_by(PaperPosition.id).all()


# ---- entry ------------------------------------------------------------------

def test_a_clean_signal_buys_the_half_delta_weekly_sized_by_the_risk_manager(desk):
    trader = desk.trader()
    desk.signal = buy_signal()

    trader.step()

    assert decisions(desk) == [("entered", rules.ENTERED)]
    [pos] = positions(desk)
    assert pos.status == "open" and pos.option_type == "CE" and pos.expiry == EXPIRY
    assert abs(pos.strike - 24000) <= 50
    assert pos.lot_size == 65 and pos.quantity == pos.lots * 65 and pos.lots >= 1
    assert pos.premium_stop < pos.premium_entry < pos.premium_target
    assert pos.risk_amount <= 350_000 * 0.01 + 1e-6
    assert pos.premium_entry * pos.quantity <= 350_000 * 0.20
    detail = pos.detail
    assert 0.45 <= detail["pick"]["delta"] <= 0.60
    assert detail["fill"] == "paper: bought at the live ask"
    assert detail["pick"]["ask"] == pos.premium_entry


def test_a_sell_signal_buys_a_put(desk):
    trader = desk.trader()
    desk.signal = {**buy_signal(stop=24050.0, target=23890.0), "action": "SELL",
                   "plan": {"bias": {"label": "BEARISH"}, "entry": {"state": "ENTER_NOW"}}}
    trader.step()
    assert positions(desk)[0].option_type == "PE"


def test_the_same_signal_is_considered_once(desk):
    trader = desk.trader()
    desk.signal = buy_signal(action="HOLD")
    desk.signal["action"] = "HOLD"
    trader.step()
    trader.step()
    assert decisions(desk) == [("rejected", rules.HOLD)]


def test_a_signal_published_before_start_is_never_acted_on(desk, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("V2_PAPER_ENABLED", "true")
    get_settings.cache_clear()
    desk.signal = buy_signal()
    trader = desk.trader()
    assert trader.start() is True
    trader.stop()
    trader.step()
    assert decisions(desk) == [] and positions(desk) == []
    get_settings.cache_clear()


@pytest.mark.parametrize("change,code", [
    (lambda d: setattr(d, "kill", True), rules.KILL_SWITCH),
    (lambda d: setattr(d, "feed_ok", False), rules.FEED_DOWN),
    (lambda d: setattr(d, "spot", None), rules.NO_SPOT),
    (lambda d: setattr(d, "vix", 15.9), rules.VIX_HIGH),
    (lambda d: setattr(d, "vix", None), rules.VIX_NO_LIVE),
    (lambda d: d.listed.insert(0, date(2026, 9, 17)), rules.EXPIRY_DAY),
    (lambda d: d.clock.at(14, 45), rules.OUTSIDE_WINDOW),
    (lambda d: setattr(d, "listed", []), rules.CHAIN_NOT_READY),
])
def test_every_gate_files_its_reason(desk, change, code):
    trader = desk.trader()
    change(desk)
    desk.signal = buy_signal()
    trader.step()
    assert decisions(desk) == [("rejected", code)]
    assert positions(desk) == []


def test_a_spiking_vix_blocks(desk):
    with desk.session() as db:
        db.add(VixDaily(session_date=date(2026, 9, 16), close=10.2, source="test"))
        db.commit()
    desk.vix = 11.5                                   # +12.7% on the day
    trader = desk.trader()
    desk.signal = buy_signal()
    trader.step()
    assert decisions(desk) == [("rejected", rules.VIX_SPIKE)]


def test_without_vix_history_nothing_is_bought(desk):
    with desk.session() as db:
        db.query(VixDaily).delete()
        db.commit()
    trader = desk.trader()
    desk.signal = buy_signal()
    trader.step()
    assert decisions(desk) == [("rejected", rules.VIX_NO_HISTORY)]


def test_an_index_already_past_the_stop_is_refused(desk):
    desk.spot = 23940.0
    trader = desk.trader()
    desk.signal = buy_signal()
    trader.step()
    assert decisions(desk) == [("rejected", rules.PAST_STOP)]


def test_a_contract_with_no_listed_lot_size_is_never_sized_by_assumption(desk):
    rows = two_expiry_master()
    for row in rows:
        row["lotsize"] = ""
    desk.chain.set_universe(ou.build(rows, 24000.0, band=10, expiry=EXPIRY))
    desk.quote_all()
    trader = desk.trader()
    desk.signal = buy_signal()
    trader.step()
    assert decisions(desk) == [("rejected", rules.CHAIN_NOT_READY)]


def test_a_small_account_is_refused_by_the_risk_manager_with_the_capital_it_needs(desk):
    trader = desk.trader(V2Config(paper_capital=20_000))
    desk.signal = buy_signal()
    trader.step()
    assert decisions(desk) == [("rejected", rules.RISK_VETO)]
    with desk.session() as db:
        detail = db.query(PaperDecision).one().detail
    assert "capital" in detail["detail"]


def test_a_thin_reward_is_refused_by_the_reward_to_risk_floor(desk):
    trader = desk.trader()
    desk.signal = buy_signal(stop=23950.0, target=24060.0)
    trader.step()
    assert decisions(desk) == [("rejected", rules.RISK_VETO)]


# ---- the open position ----------------------------------------------------------

def open_position(desk):
    trader = desk.trader()
    desk.signal = buy_signal()
    trader.step()
    [pos] = positions(desk)
    return trader, pos


def test_a_second_signal_while_open_is_refused(desk):
    trader, _ = open_position(desk)
    desk.clock.advance(minutes=5)
    desk.quote_all()
    desk.signal = buy_signal(confidence=0.7)
    trader.step()
    assert decisions(desk)[-1] == ("rejected", rules.POSITION_OPEN)


def test_the_premium_stop_closes_at_the_live_bid_and_charges_costs(desk):
    trader, pos = open_position(desk)
    desk.clock.advance(minutes=3)
    desk.tick(pos.token, pos.premium_stop - 1.0)

    trader.step()

    [closed] = positions(desk)
    assert closed.status == "closed" and closed.exit_reason == rules.EXIT_PREMIUM_STOP
    assert closed.premium_exit == pytest.approx(pos.premium_stop - 1.05, abs=0.01)
    assert closed.costs > 0
    assert closed.pnl == pytest.approx(closed.gross_pnl - closed.costs)
    assert closed.pnl < 0 and closed.r_multiple < -1
    assert closed.detail["exit"]["priced_from"] == "live_bid"


def test_the_index_target_closes_the_position_at_a_profit(desk):
    trader, pos = open_position(desk)
    desk.clock.advance(minutes=20)
    desk.spot = 24110.0
    desk.quote_all(spot=24110.0)

    trader.step()

    [closed] = positions(desk)
    assert closed.exit_reason in (rules.EXIT_INDEX_TARGET, rules.EXIT_PREMIUM_TARGET)
    assert closed.pnl > 0


def test_a_quiet_position_is_closed_at_the_session_exit(desk):
    trader, pos = open_position(desk)
    desk.clock.at(15, 15)
    desk.quote_all()
    trader.step()
    assert positions(desk)[0].exit_reason == rules.EXIT_SESSION_END


def test_a_stale_quote_never_triggers_a_premium_exit(desk):
    trader, pos = open_position(desk)
    desk.tick(pos.token, pos.premium_stop - 5)        # printed now...
    desk.clock.advance(minutes=2)                     # ...and two minutes stale
    trader.step()
    assert positions(desk)[0].status == "open"


def test_manual_close_uses_the_bid(desk):
    trader, pos = open_position(desk)
    closed = trader.close_now()
    assert closed["exit_reason"] == rules.EXIT_MANUAL
    assert positions(desk)[0].status == "closed"
    assert trader.close_now() is None


def test_the_daily_trade_cap_applies_to_paper_trades(desk):
    trader = desk.trader()
    for n in range(2):
        desk.signal = buy_signal(confidence=0.6 + n / 100)
        trader.step()
        trader.close_now()
        desk.clock.advance(minutes=5)
        desk.quote_all()
    desk.signal = buy_signal(confidence=0.9)
    trader.step()
    with desk.session() as db:
        last = db.query(PaperDecision).order_by(PaperDecision.id.desc()).first()
    assert last.code == rules.RISK_VETO and "cap" in last.detail["detail"]


def test_a_position_stranded_by_a_restart_is_closed_and_labelled(desk):
    trader, pos = open_position(desk)
    desk.clock.advance(days=1)
    desk.clock.at(9, 20)

    fresh = desk.trader()
    fresh.recover()

    [closed] = positions(desk)
    assert closed.exit_reason == rules.EXIT_RESTART
    assert closed.detail["exit"]["priced_from"] == "last_seen_before_restart"
    assert fresh._open_id is None


def test_a_restart_inside_the_session_resumes_monitoring(desk):
    trader, pos = open_position(desk)
    fresh = desk.trader()
    fresh.recover()
    assert fresh._open_id == pos.id
    desk.clock.advance(minutes=1)
    desk.tick(pos.token, pos.premium_stop - 1.0)
    fresh.step()
    assert positions(desk)[0].exit_reason == rules.EXIT_PREMIUM_STOP


# ---- the account and the published state --------------------------------------------

def test_equity_carries_realised_pnl_into_the_next_trade(desk):
    trader, pos = open_position(desk)
    desk.clock.advance(minutes=1)
    desk.tick(pos.token, pos.premium_stop - 1.0)
    trader.step()
    loss = positions(desk)[0].pnl

    state = trader.status()

    assert state["account"]["equity"] == pytest.approx(350_000 + loss)
    assert state["account"]["realised_today"] == pytest.approx(loss)
    assert state["account"]["consecutive_losses"] == 1
    assert state["mode"] == "paper"


def test_status_shows_the_open_position_with_live_unrealised_pnl(desk):
    trader, pos = open_position(desk)
    desk.clock.advance(seconds=5)
    desk.quote_all()
    trader.step()

    state = trader.status()

    live = state["position"]["live"]
    assert live["bid"] is not None and "unrealised" in live
    assert state["gates"]["expiry"] == EXPIRY.isoformat()
    assert state["gates"]["vix"]["ok"] is True
    assert state["decisions_today"] == {rules.ENTERED: 1}
    assert desk.published, "state was never published"


def test_the_session_vix_close_is_filed_once_after_the_close(desk, monkeypatch):
    from app.workers import vix_live
    store = vix_live.VixStore(clock=desk.clock, publish=lambda blob: None)
    monkeypatch.setattr(vix_live, "VIX", store)
    desk.clock.at(15, 31)
    store.update(type("T", (), {"price": 13.37, "source_time": desk.clock()})())
    trader = desk.trader()

    trader.step()
    trader.step()

    with desk.session() as db:
        rows = db.query(VixDaily).filter(VixDaily.session_date == date(2026, 9, 17)).all()
    assert [(r.close, r.source) for r in rows] == [(13.37, "angel_live")]


def test_fingerprints_change_when_the_decision_does():
    a = buy_signal()
    assert fingerprint(a) == fingerprint(dict(a))
    assert fingerprint(a) != fingerprint({**a, "price": 24001.0})
    assert fingerprint(None) is None
