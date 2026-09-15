"""Strategy v2 on paper: take the desk's signal, buy a simulated option.

What happens, in order, whenever the desk publishes a new signal:

  1. The signal itself: a direction with levels, ENTER_NOW, bias agreeing.
  2. The session: inside the entry window, not an expiry day, kill switch
     off, no position already open, the Angel feed live.
  3. India VIX: not above the 80th percentile of its last year, and not up
     10% on the day.
  4. The contract: the nearest weekly with two sessions left, the liquid
     strike nearest 0.50 delta, from live Angel quotes.
  5. The levels and the size: premium stop and target from the index
     levels and the 30% / 50% caps, sized by the desk's own risk manager
     against the simulated account.

Every refusal is filed with its reason, the same as every entry. The open
position is then checked about once a second against the live bid and the
index, and closed by the first exit rule that fires.

The fills are paper and say so: bought at the live ask, sold at the live
bid, charged the desk's cost model. That is kinder than a real market
order in a fast tape and harsher than a patient limit order, and neither
difference is hidden.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import nullcontext
from datetime import UTC, date, datetime

from sqlalchemy import func, select

from .. import killswitch, market_hours
from ..analytics import option_pricing
from ..backtest.costs import CostModel
from ..cache import get_json, publish
from ..config import get_settings
from ..db import SessionLocal
from ..market_hours import IST
from ..models import PaperDecision, PaperPosition
from ..optionbuy.contracts import expiry_moment
from ..risk.manager import DayState, RiskConfig, day_state_from_trades, evaluate
from ..workers import option_chain_live, vix_live
from . import rules
from . import vix as vix_history
from .config import DEFAULT, NAME, Rejection, V2Config

log = logging.getLogger(__name__)

CHANNEL = "v2"
CACHE_KEY = "v2:latest"

# How often the loop runs. Exits are checked on every pass.
STEP_SECONDS = 1.0
# A premium older than this is not used to trigger or price an exit.
EXIT_QUOTE_MAX_AGE = 30.0
# The position row is written at most this often while it is open; the
# live figures ride the published state in between.
PERSIST_EVERY_SECONDS = 5.0
# When nothing is open the published state changes slowly, and outside the
# session hardly at all.
IDLE_PUBLISH_SECONDS = 5.0
CLOSED_PUBLISH_SECONDS = 60.0


def _aware(moment: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is written as UTC."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def fingerprint(signal: dict | None) -> str | None:
    if not signal:
        return None
    return "|".join(str(signal.get(k)) for k in
                    ("timestamp", "action", "price", "confidence", "stop_loss", "target"))


def _live_spot() -> float | None:
    """NIFTY now: the Angel feed when it is live, else a fresh cached price."""
    from ..workers import angel_feed
    if angel_feed.healthy() and angel_feed.FEED.stats.last_price:
        return float(angel_feed.FEED.stats.last_price)
    cached = get_json("price:latest") or {}
    try:
        stamp = datetime.fromisoformat(str(cached.get("source_time")))
        if (datetime.now(UTC) - _aware(stamp)).total_seconds() <= 15:
            return float(cached["price"])
    except (TypeError, ValueError, KeyError):
        pass
    return None


def _feed_healthy() -> bool:
    from ..workers import angel_feed
    return angel_feed.healthy()


def _publish_state(payload: dict) -> None:
    publish(CHANNEL, json.dumps(payload, default=str), cache_key=CACHE_KEY, ttl=120)


class PaperTrader:
    def __init__(self, *, cfg: V2Config = DEFAULT, session_factory=SessionLocal,
                 clock=None, signal_source=None, spot_source=None, vix_source=None,
                 chain_lookup=None, listed_expiries=None, feed_healthy=None,
                 kill_switch=None, publish_fn=None, market_open=None,
                 costs: CostModel | None = None) -> None:
        # Seams for every input, so the whole decision can be driven by a
        # test at 10:30 on a Thursday without a socket, a broker or Redis.
        self.cfg = cfg
        self._session = session_factory
        self._now = clock or (lambda: datetime.now(UTC))
        self._signal = signal_source or (lambda: get_json("signal:latest"))
        self._spot = spot_source or _live_spot
        self._vix = vix_source or (lambda: vix_live.VIX.current())
        self._chain_for = chain_lookup or option_chain_live.chain_for
        self._listed = listed_expiries or option_chain_live.LISTED.get
        self._feed_ok = feed_healthy or _feed_healthy
        self._kill = kill_switch or killswitch.engaged
        self._publish = publish_fn or _publish_state
        self._market_open = market_open or market_hours.is_open
        self._costs = costs or CostModel()

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._seen: str | None = None
        self._open_id: int | None = None
        self._live: dict = {}                 # bid, spot, quote age for the open position
        self._persisted_at: datetime | None = None
        self._published_at: datetime | None = None
        self._vix_recorded_for: date | None = None
        self.last_decision: dict | None = None
        self.errors = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        if not get_settings().v2_paper_enabled:
            log.info("strategy v2 paper trading is off (V2_PAPER_ENABLED)")
            return False
        if self._thread and self._thread.is_alive():
            return True
        # Whatever signal is cached now was published before this process
        # started, possibly fifteen minutes ago. Acting on it at boot would
        # open a position on a decision nobody made at this price.
        self._seen = fingerprint(self._signal())
        self.recover()
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name="v2-paper", daemon=True)
        self._thread.start()
        log.info("strategy v2 paper trader running")
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stopping.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.step()
            except Exception:                              # noqa: BLE001
                self.errors += 1
                log.exception("v2 paper step failed")
            self._stopping.wait(STEP_SECONDS)

    # ---- one pass ----------------------------------------------------------

    def step(self) -> None:
        now = self._now()
        with self._lock:
            if self._open_id is not None:
                self._monitor(now)

            signal = self._signal()
            mark = fingerprint(signal)
            if mark is not None and mark != self._seen:
                self._seen = mark
                if self._market_open(now):
                    self.consider(signal, now)

            self._record_vix_close(now)
            self._maybe_publish(now)

    def recover(self) -> None:
        """Resume the open position after a restart, or close a stranded one."""
        now = self._now()
        with self._session() as db:
            row = db.scalar(select(PaperPosition).where(
                PaperPosition.strategy == NAME, PaperPosition.status == "open")
                .order_by(PaperPosition.id.desc()))
            if row is None:
                return
            ist = now.astimezone(IST)
            stranded = (row.session_date != ist.date()
                        or ist.time() >= self.cfg.session_exit
                        or not self._market_open(now))
            if stranded:
                # The session it belonged to is over. Priced at the last
                # premium the trader saw, and labelled so — no quote exists
                # for the moment it should have been closed.
                self._close(db, row, now, rules.EXIT_RESTART,
                            premium=row.last_premium or row.premium_entry, spot=None,
                            priced_from="last_seen_before_restart")
            else:
                self._open_id = row.id
                log.info("v2 resumed open paper position %s (%s)", row.id, row.contract)

    # ---- entry ---------------------------------------------------------------

    def consider(self, signal: dict, now: datetime) -> dict:
        """Run one signal through every rule. Returns the filed decision."""
        cfg = self.cfg
        sig = dict(signal)
        sig.setdefault("generated_at", now.isoformat())
        ist = now.astimezone(IST)
        today = ist.date()
        action = str(sig.get("action"))

        with self._session() as db:
            def refuse(rejection: Rejection, **extra) -> dict:
                return self._file(db, now, sig, "rejected", rejection.code,
                                  {"detail": rejection.detail, **rejection.data, **extra})

            rejected = rules.signal_rejection(sig, now=now, cfg=cfg)
            if rejected:
                return refuse(rejected)
            if self._open_id is not None:
                return refuse(Rejection(rules.POSITION_OPEN, "one paper position at a time"))
            if self._kill():
                return refuse(Rejection(rules.KILL_SWITCH, "kill switch is on"))
            if not rules.in_entry_window(ist, cfg):
                return refuse(Rejection(
                    rules.OUTSIDE_WINDOW,
                    f"{ist:%H:%M} is outside {cfg.entry_start:%H:%M}–{cfg.entry_end:%H:%M}"))

            listed = self._listed()
            if not listed:
                return refuse(Rejection(rules.CHAIN_NOT_READY,
                                        "the instrument master has not been read yet"))
            if cfg.no_entry_on_expiry_day and rules.is_expiry_day(today, listed):
                return refuse(Rejection(rules.EXPIRY_DAY, f"{today} is a NIFTY expiry day"))
            if not self._feed_ok():
                return refuse(Rejection(rules.FEED_DOWN,
                                        "the Angel feed is not live; no quote to trust"))
            spot = self._spot()
            if spot is None:
                return refuse(Rejection(rules.NO_SPOT, "no live NIFTY price"))

            stop, target = float(sig["stop_loss"]), float(sig["target"])
            rejected = rules.level_rejection(action, spot, stop, target)
            if rejected:
                return refuse(rejected)

            reading = rules.vix_gate(vix_history.load_closes(db, before=today),
                                     self._vix(), cfg)
            if not reading.ok:
                return refuse(Rejection(reading.code, reading.detail),
                              vix=reading.to_dict())

            expiry = rules.choose_expiry(listed, today, cfg)
            if expiry is None:
                return refuse(Rejection(rules.NO_EXPIRY,
                                        f"no listed expiry has {cfg.min_sessions_to_expiry} "
                                        "sessions left"))
            store = self._chain_for(expiry)
            if store is None:
                return refuse(Rejection(rules.CHAIN_NOT_READY,
                                        f"{expiry} is not being streamed yet"))

            option_type = rules.option_type_for(action)
            candidates = [self._candidate(q, now) for q in store.quotes(now=now)]
            years = option_pricing.years_to_expiry(ist, expiry_moment(expiry))
            pick, rejected = rules.pick_contract(candidates, option_type=option_type,
                                                 spot=spot, years=years, cfg=cfg)
            if rejected:
                return refuse(rejected, expiry=expiry.isoformat())

            chosen = pick.candidate
            if not chosen.lot_size:
                return refuse(Rejection(rules.CHAIN_NOT_READY,
                                        f"no lot size listed for {chosen.symbol}; "
                                        "a size is never assumed"))

            entry = float(chosen.ask)
            levels = rules.premium_levels(entry=entry, strike=chosen.strike,
                                          option_type=option_type, years=years, iv=pick.iv,
                                          index_stop=stop, index_target=target, cfg=cfg)
            if not (levels.stop < entry < levels.target):
                return refuse(Rejection(rules.NO_DEFINED_RISK,
                                        f"stop {levels.stop:.2f} / target {levels.target:.2f} "
                                        f"do not straddle the {entry:.2f} entry"))

            equity = self._equity(db)
            state = self._day_state(db, today)
            risk_cfg = RiskConfig(
                capital=equity, risk_per_trade_pct=cfg.risk_per_trade_pct,
                max_trades_per_day=cfg.max_trades_per_day,
                min_risk_reward=cfg.min_risk_reward,
                max_daily_loss_pct=cfg.max_daily_loss_pct,
                max_consecutive_losses=cfg.max_consecutive_losses,
                max_open_positions=cfg.max_open_positions,
                lot_size=int(chosen.lot_size),
                max_capital_deployed_pct=cfg.max_capital_deployed_pct)
            decision = evaluate(config=risk_cfg, state=state, entry=entry,
                                stop_loss=levels.stop, target=levels.target,
                                unit_cost=entry)
            context = {"expiry": expiry.isoformat(), "spot": spot, "vix": reading.to_dict(),
                       "pick": pick.to_dict(), "levels": levels.to_dict(),
                       "risk": decision.to_dict(), "equity": round(equity, 2)}
            if not decision.approved:
                return refuse(Rejection(rules.RISK_VETO, "; ".join(decision.reasons)),
                              **context)

            row = PaperPosition(
                strategy=NAME, status="open", session_date=today, opened_at=now,
                direction=action,
                contract=f"NIFTY {expiry:%d%b%y} {chosen.strike:.0f} {option_type}".upper(),
                token=chosen.token, option_type=option_type, strike=chosen.strike,
                expiry=expiry, lot_size=int(chosen.lot_size), lots=decision.lots,
                quantity=decision.quantity, index_entry=spot, index_stop=stop,
                index_target=target, premium_entry=entry, premium_stop=levels.stop,
                premium_target=levels.target,
                risk_amount=round((entry - levels.stop) * decision.quantity, 2),
                last_premium=chosen.bid, best_premium=chosen.bid, worst_premium=chosen.bid,
                detail={**context, "signal": _signal_summary(sig),
                        "fill": "paper: bought at the live ask"},
            )
            db.add(row)
            db.flush()
            self._open_id = row.id
            self._persisted_at = now
            filed = self._file(db, now, sig, "entered", rules.ENTERED,
                               {"position_id": row.id, "contract": row.contract,
                                "lots": row.lots, "entry": entry, **context})
            log.info("v2 paper entry: %s x%s at %.2f (stop %.2f, target %.2f)",
                     row.contract, row.quantity, entry, levels.stop, levels.target)
            self._published_at = None
            return filed

    @staticmethod
    def _candidate(quote, now: datetime) -> rules.Candidate:
        c = quote.contract
        return rules.Candidate(strike=c.strike, option_type=c.option_type, ltp=quote.price,
                               bid=quote.bid, ask=quote.ask,
                               age_seconds=quote.age_seconds(now), token=c.token,
                               symbol=c.symbol, lot_size=c.lot_size)

    # ---- monitoring and exit ------------------------------------------------

    def _quote_for(self, row: PaperPosition, now: datetime):
        store = self._chain_for(row.expiry)
        if store is None:
            return None
        for quote in store.quotes(now=now):
            if quote.contract.token == row.token:
                return quote
        return None

    def _monitor(self, now: datetime) -> None:
        with self._session() as db:
            row = db.get(PaperPosition, self._open_id)
            if row is None or row.status != "open":
                self._open_id = None
                return

            quote = self._quote_for(row, now)
            bid = None
            age = None
            if quote is not None:
                age = quote.age_seconds(now)
                if quote.bid and quote.bid > 0 and age <= EXIT_QUOTE_MAX_AGE:
                    bid = float(quote.bid)
            spot = self._spot()
            self._live = {"bid": bid, "spot": spot,
                          "quote_age_seconds": round(age, 2) if age is not None else None,
                          "at": now.isoformat()}

            if bid is not None:
                row.last_premium = bid
                row.best_premium = max(row.best_premium or bid, bid)
                row.worst_premium = min(row.worst_premium or bid, bid)

            ist = now.astimezone(IST)
            reason = rules.exit_reason(
                direction=row.direction, bid=bid, spot=spot,
                premium_stop=row.premium_stop, premium_target=row.premium_target,
                index_stop=row.index_stop, index_target=row.index_target,
                now_ist=ist, opened_at_ist=_aware(row.opened_at).astimezone(IST),
                cfg=self.cfg)
            if reason is not None:
                premium = bid if bid is not None else (row.last_premium or row.premium_entry)
                self._close(db, row, now, reason, premium=premium, spot=spot,
                            priced_from="live_bid" if bid is not None else "last_seen")
                return

            if (self._persisted_at is None or
                    (now - self._persisted_at).total_seconds() >= PERSIST_EVERY_SECONDS):
                db.commit()
                self._persisted_at = now

    def close_now(self, reason: str = rules.EXIT_MANUAL) -> dict | None:
        """Close the open paper position at the current bid, if there is one."""
        now = self._now()
        with self._lock, self._session() as db:
            if self._open_id is None:
                return None
            row = db.get(PaperPosition, self._open_id)
            if row is None or row.status != "open":
                self._open_id = None
                return None
            quote = self._quote_for(row, now)
            bid = float(quote.bid) if quote is not None and quote.bid else None
            premium = bid if bid is not None else (row.last_premium or row.premium_entry)
            self._close(db, row, now, reason, premium=premium, spot=self._spot(),
                        priced_from="live_bid" if bid is not None else "last_seen")
            return position_dict(row)

    def _close(self, db, row: PaperPosition, now: datetime, reason: str, *,
               premium: float, spot: float | None, priced_from: str) -> None:
        charges = self._costs.round_trip(buy_price=row.premium_entry, sell_price=premium,
                                         quantity=row.quantity)
        gross = (premium - row.premium_entry) * row.quantity
        pnl = gross - charges.total
        row.status = "closed"
        row.closed_at = now
        row.premium_exit = round(premium, 2)
        row.index_exit = spot
        row.exit_reason = reason
        row.gross_pnl = round(gross, 2)
        row.costs = round(charges.total, 2)
        row.pnl = round(pnl, 2)
        row.r_multiple = round(pnl / row.risk_amount, 3) if row.risk_amount else None
        row.detail = {**(row.detail or {}), "exit": {
            "reason": reason, "priced_from": priced_from, "costs": charges.to_dict(),
            "fill": "paper: sold at the live bid" if priced_from == "live_bid"
                    else "paper: no live bid, priced at the last premium seen"}}
        db.commit()
        log.info("v2 paper exit: %s %s at %.2f, pnl %.2f", row.contract, reason, premium, pnl)
        if self._open_id == row.id:
            self._open_id = None
        self._live = {}
        self._published_at = None

    # ---- the account ------------------------------------------------------------

    def _equity(self, db) -> float:
        realised = db.scalar(select(func.coalesce(func.sum(PaperPosition.pnl), 0.0)).where(
            PaperPosition.strategy == NAME, PaperPosition.status == "closed")) or 0.0
        return self.cfg.paper_capital + float(realised)

    def _day_state(self, db, today: date) -> DayState:
        todays = db.scalars(select(PaperPosition).where(
            PaperPosition.strategy == NAME, PaperPosition.session_date == today)
            .order_by(PaperPosition.id)).all()
        rows = [_Journal(r) for r in todays]
        return day_state_from_trades(today, rows, [r for r in rows if r.status == "open"])

    # ---- records and publishing ---------------------------------------------------

    def _file(self, db, now: datetime, sig: dict, outcome: str, code: str,
              detail: dict) -> dict:
        ist = now.astimezone(IST)
        record = PaperDecision(strategy=NAME, decided_at=now, session_date=ist.date(),
                               signal_time=str(sig.get("timestamp") or "")[:40] or None,
                               action=str(sig.get("action") or "")[:8], outcome=outcome,
                               code=code, detail=json.loads(json.dumps(detail, default=str)))
        db.add(record)
        db.commit()
        self.last_decision = {"at": now.isoformat(), "action": record.action,
                              "outcome": outcome, "code": code, **record.detail}
        self._published_at = None
        return self.last_decision

    def _record_vix_close(self, now: datetime) -> None:
        ist = now.astimezone(IST)
        if ist.time() < market_hours.MARKET_CLOSE or self._vix_recorded_for == ist.date():
            return
        reading = vix_live.VIX.last()
        if reading is None or reading.source_time.astimezone(IST).date() != ist.date():
            return
        if not market_hours.is_trading_date(ist.date()):
            return
        with self._session() as db:
            vix_history.record_close(db, ist.date(), reading.value)
        self._vix_recorded_for = ist.date()

    def _maybe_publish(self, now: datetime) -> None:
        if self._open_id is not None:
            every = STEP_SECONDS
        elif self._market_open(now):
            every = IDLE_PUBLISH_SECONDS
        else:
            every = CLOSED_PUBLISH_SECONDS
        if (self._published_at is not None and
                (now - self._published_at).total_seconds() < every):
            return
        self._published_at = now
        try:
            self._publish(self.status(now))
        except Exception as exc:                             # noqa: BLE001
            log.debug("v2 publish failed: %s", exc)

    def status(self, now: datetime | None = None, db=None) -> dict:
        now = now or self._now()
        ist = now.astimezone(IST)
        today = ist.date()
        cfg = self.cfg
        with (nullcontext(db) if db is not None else self._session()) as db:
            equity = self._equity(db)
            state = self._day_state(db, today)
            open_row = db.get(PaperPosition, self._open_id) if self._open_id else None
            closed_today = db.scalars(select(PaperPosition).where(
                PaperPosition.strategy == NAME, PaperPosition.status == "closed",
                PaperPosition.session_date == today)).all()
            counts = dict(db.execute(select(PaperDecision.code, func.count()).where(
                PaperDecision.strategy == NAME, PaperDecision.session_date == today)
                .group_by(PaperDecision.code)).all())
            history = vix_history.load_closes(db, before=today)

        listed = self._listed()
        expiry = rules.choose_expiry(listed, today, cfg) if listed else None
        store = self._chain_for(expiry) if expiry else None
        position = None
        if open_row is not None:
            position = position_dict(open_row)
            bid = self._live.get("bid")
            if bid is not None:
                gross = (bid - open_row.premium_entry) * open_row.quantity
                position["live"] = {**self._live,
                                    "unrealised": round(gross, 2),
                                    "r": round(gross / open_row.risk_amount, 2)
                                    if open_row.risk_amount else None}
            else:
                position["live"] = self._live or None

        return {
            "strategy": NAME, "mode": "paper", "running": self.running,
            "at": now.isoformat(),
            "account": {
                "starting_capital": cfg.paper_capital,
                "equity": round(equity, 2),
                "realised_total": round(equity - cfg.paper_capital, 2),
                "realised_today": round(sum(r.pnl or 0 for r in closed_today), 2),
                "trades_today": state.trades_taken,
                "consecutive_losses": state.consecutive_losses,
                "max_trades_per_day": cfg.max_trades_per_day,
            },
            "position": position,
            "gates": {
                "market_open": self._market_open(now),
                "entry_window": rules.in_entry_window(ist, cfg),
                "expiry_day": rules.is_expiry_day(today, listed) if listed else None,
                "expiry": expiry.isoformat() if expiry else None,
                "chain_quotes": len(store.quotes(now=now)) if store else 0,
                "feed_live": self._feed_ok(),
                "kill_switch": self._kill(),
                "vix": rules.vix_gate(history, self._vix(), cfg).to_dict(),
            },
            "last_decision": self.last_decision,
            "decisions_today": counts,
            "config": cfg.to_dict(),
        }


class _Journal:
    """A paper position in the shape `day_state_from_trades` reads."""

    def __init__(self, row: PaperPosition) -> None:
        self.status = row.status
        self.pnl = row.pnl
        self.created_at = _aware(row.closed_at or row.opened_at)


def _signal_summary(sig: dict) -> dict:
    plan = sig.get("plan") or {}
    return {k: sig.get(k) for k in ("timestamp", "generated_at", "action", "confidence",
                                    "price", "entry", "stop_loss", "target", "risk_reward")} | {
        "bias": (plan.get("bias") or {}).get("label"),
        "entry_state": (plan.get("entry") or {}).get("state")}


def position_dict(row: PaperPosition) -> dict:
    return {
        "id": row.id, "status": row.status, "contract": row.contract,
        "direction": row.direction, "option_type": row.option_type, "strike": row.strike,
        "expiry": row.expiry.isoformat() if row.expiry else None,
        "session_date": row.session_date.isoformat() if row.session_date else None,
        "opened_at": _aware(row.opened_at).isoformat() if row.opened_at else None,
        "closed_at": _aware(row.closed_at).isoformat() if row.closed_at else None,
        "lots": row.lots, "lot_size": row.lot_size, "quantity": row.quantity,
        "index_entry": row.index_entry, "index_stop": row.index_stop,
        "index_target": row.index_target,
        "premium_entry": row.premium_entry, "premium_stop": round(row.premium_stop, 2),
        "premium_target": round(row.premium_target, 2), "risk_amount": row.risk_amount,
        "last_premium": row.last_premium, "best_premium": row.best_premium,
        "worst_premium": row.worst_premium, "premium_exit": row.premium_exit,
        "index_exit": row.index_exit, "exit_reason": row.exit_reason,
        "gross_pnl": row.gross_pnl, "costs": row.costs, "pnl": row.pnl,
        "r_multiple": row.r_multiple,
    }


TRADER = PaperTrader()


def start() -> bool:
    return TRADER.start()


def stop() -> None:
    TRADER.stop()

