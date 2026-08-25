"""Every live signal carries a risk decision — on both routes.

Audit finding H-4. Signals reach the dashboard two ways: the agent publishes
to Redis and the websocket relays that, while `/signals/live` is a
sixty-second fallback used only when the socket is down. The risk block was
assembled inline in the endpoint, so the fallback carried a decision and the
primary route carried none — `evaluate()` was never called on it.

The failure was silent in every direction. `PlanPanel` guards its risk rows
with `{risk && ...}`, so a missing block rendered a clean trade plan with
entry, stop and target and no hint the trade had been refused. Observed on
the running stack: `redis-cli GET signal:latest` returned `"action": "BUY"`
with no `risk` key at all, which meant the kill switch, the daily trade cap,
the loss limit, the consecutive-loss rule and the open-position cap changed
nothing the desk could see.

So these tests are mostly about the *publish* path, not the endpoint: the
endpoint was already right and stayed right.
"""
import ast
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics.signal_engine import Signal
from app.api import signals as signals_api
from app.api.signals import Analysis
from app.config import get_settings
from app.data import repository
from app.db import get_db
from app.market_hours import IST, trading_date
from app.models import TradeRecord
from app.risk import live as risk_live
from app.workers import agent

# ---- harness ----------------------------------------------------------

class SessionFactory:
    """Hands `agent.tick()` the test's own session.

    A context manager that does not close on exit — the fixture owns the
    session's lifetime, and closing it here would detach every row the
    assertions still need.
    """

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def a_buy(entry=24_200.0, stop=24_190.0, target=24_225.0):
    """A signal that proposes a real trade, so risk has something to judge.

    Deliberately hand-built rather than coaxed out of the signal engine:
    what is under test is whether a decision is attached, not how the
    decision to buy was reached. Nothing here touches signal weights.

    The stop is ten points out on purpose. At the default 100,000 capital
    and 1% per trade, a wider stop cannot afford a single 75-lot and the
    trade is refused on sizing alone — which would make the "approved" case
    below untestable and every "blocked" case pass for the wrong reason.
    """
    return Signal(
        symbol="NIFTY", timeframe="5m",
        timestamp=datetime.now(UTC).isoformat(),
        action="BUY", confidence=0.62, price=entry,
        entry=entry, stop_loss=stop, target=target,
        risk_reward=round((target - entry) / (entry - stop), 2),
        checks=[], context={"trend": "up"},
    )


def a_hold():
    return Signal(
        symbol="NIFTY", timeframe="5m",
        timestamp=datetime.now(UTC).isoformat(),
        action="HOLD", confidence=0.20, price=24_200.0,
        checks=[], context={},
    )


def closed_trade(pnl, order=0):
    """A trade closed earlier on today's IST trading day.

    Anchored to IST midnight of the current trading date, not to
    `now - N minutes`. The daily limits count by IST *trading date*, so a
    relative offset silently lands on yesterday whenever the suite runs in
    the first hour of an IST day — and CI does exactly that: its Test step
    ran at 19:19 UTC, which is 00:49 IST. A loss dated sixty minutes earlier
    fell onto the previous day, the streak counted one instead of two,
    nothing tripped, and the assertion failed with `'approved' == 'blocked'`
    and no hint that the clock was responsible.

    Anchoring forward from midnight keeps every row on today's date at any
    hour the suite happens to run. `order` only sequences them: the
    consecutive-loss walk reads closed trades newest-first, so a streak
    needs distinct, increasing timestamps.
    """
    midnight = datetime.combine(trading_date(), time(0, 0), tzinfo=IST)
    return TradeRecord(
        symbol="NIFTY", side="BUY", quantity=75,
        entry=24_200.0, stop_loss=24_190.0, status="closed", pnl=pnl,
        created_at=(midnight + timedelta(seconds=order)).astimezone(UTC),
    )


def open_trade():
    return TradeRecord(
        symbol="NIFTY", side="BUY", quantity=75,
        entry=24_200.0, stop_loss=24_190.0, status="open", pnl=None,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def published(db, monkeypatch):
    """Run `agent.tick()` and capture what it hands to Redis.

    Returns a callable: give it a signal, get back the published payload.
    """
    sent = {}

    def capture(channel, blob, **kw):
        sent["channel"] = channel
        sent["payload"] = json.loads(blob)
        return True

    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish", capture)

    def run(signal):
        # The agent builds a signal and a two-layer plan in one pass now.
        # Stubbing the pass rather than the signal keeps the seam where the
        # code actually has one; `plan=None` is the honest stand-in, since
        # what is under test here is the risk decision, not the plan.
        monkeypatch.setattr(agent, "build_analysis",
                            lambda *a, **k: Analysis(signal=signal, plan=None))
        agent.tick(force=True)
        assert sent, "the agent published nothing"
        return sent["payload"]

    return run


# ---- the helper's own contract -----------------------------------------

def test_seeded_trades_always_land_on_the_day_they_claim(monkeypatch):
    """Why CI went red on a green suite.

    `closed_trade` used to date rows as `now - N minutes`. Run in the first
    hour of an IST day — CI's Test step ran at 19:19 UTC, which is 00:49
    IST — a row sixty minutes back fell onto *yesterday*, vanished from
    today's journal, and the limit it was there to trip measured against a
    short count. The suite passed at every hour I happened to run it.

    The date is faked rather than the clock: patching `datetime` globally to
    reproduce this hangs a suite that contains real sleeps. Anchoring is the
    property under test, and it is testable directly.
    """
    for pretend_today in (date(2026, 8, 22), date(2026, 1, 1), date(2026, 12, 31)):
        monkeypatch.setitem(globals(), "trading_date", lambda d=pretend_today: d)
        for order in range(3):
            row = closed_trade(-100.0, order=order)
            assert row.created_at.astimezone(IST).date() == pretend_today


def test_seeded_trades_are_ordered_so_a_streak_can_be_read():
    """`day_state_from_trades` walks closed trades newest-first, so rows
    sharing a timestamp make the streak order undefined."""
    rows = [closed_trade(-100.0, order=i) for i in range(3)]
    stamps = [r.created_at for r in rows]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 3


# ---- the regression ---------------------------------------------------

def test_the_websocket_payload_carries_a_risk_decision(published):
    """The exact shape that was missing. `signal:latest` held a BUY with no
    `risk` key, and that blob is what the socket relays verbatim."""
    payload = published(a_buy())

    assert "risk" in payload, "published signal has no risk decision"
    assert payload["risk"]["state"] in {"approved", "blocked"}
    assert payload["risk"]["evaluated"] is True
    assert payload["action"] == "BUY"


def test_an_approved_trade_reports_size_and_rupees_at_risk(published):
    risk = published(a_buy())["risk"]

    assert risk["state"] == "approved"
    assert risk["approved"] is True
    assert risk["quantity"] > 0
    assert risk["lots"] >= 1
    assert risk["rupees_at_risk"] > 0
    assert risk["reasons"], "an approval should still say how it was sized"


def test_the_kill_switch_reaches_the_websocket_path(published, monkeypatch):
    """The headline consequence: flipping this changed nothing on screen."""
    monkeypatch.setenv("KILL_SWITCH", "true")
    get_settings.cache_clear()

    risk = published(a_buy())["risk"]

    assert risk["state"] == "blocked"
    assert risk["approved"] is False
    assert any("Kill switch" in r for r in risk["reasons"])


def test_the_daily_trade_cap_reaches_the_websocket_path(db, published):
    db.add_all([closed_trade(500.0, order=1), closed_trade(300.0, order=2)])
    db.commit()                                              # cap is 2
    assert len(repository.todays_trades(db, trading_date())) == 2

    risk = published(a_buy())["risk"]

    assert risk["state"] == "blocked"
    assert any("trade cap" in r for r in risk["reasons"])
    assert risk["day_state"]["trades_taken"] == 2


def test_the_open_position_cap_reaches_the_websocket_path(db, published):
    db.add(open_trade())
    db.commit()

    risk = published(a_buy())["risk"]

    assert risk["state"] == "blocked"
    assert any("Already holding" in r for r in risk["reasons"])
    assert risk["day_state"]["open_positions"] == 1


def test_the_consecutive_loss_rule_reaches_the_websocket_path(db, published):
    db.add_all([closed_trade(-800.0, order=1), closed_trade(-700.0, order=2)])
    db.commit()

    # The premise, asserted before the verdict. Without it a journal that
    # lost a row to the IST date boundary reports itself as a risk-logic
    # failure rather than as the clock problem it is.
    assert len(repository.todays_trades(db, trading_date())) == 2

    risk = published(a_buy())["risk"]

    assert risk["state"] == "blocked"
    assert any("losses in a row" in r for r in risk["reasons"])
    assert risk["day_state"]["consecutive_losses"] == 2


def test_the_daily_loss_limit_reaches_the_websocket_path(db, published, monkeypatch):
    """Isolated from the other limits: one trade, one big loss, a cap raised
    so the trade count alone cannot be what blocks it."""
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "10")
    monkeypatch.setenv("MAX_CONSECUTIVE_LOSSES", "10")
    get_settings.cache_clear()

    db.add(closed_trade(-4_000.0, order=1))    # limit is 3% of 100,000
    db.commit()
    assert len(repository.todays_trades(db, trading_date())) == 1

    risk = published(a_buy())["risk"]

    assert risk["state"] == "blocked"
    assert any("loss limit" in r for r in risk["reasons"])
    assert risk["day_state"]["realised_pnl"] == pytest.approx(-4_000.0)


def test_a_hold_still_carries_a_block(published):
    """A payload whose shape depends on the verdict is one every consumer
    has to special-case — which is how the missing key went unnoticed."""
    payload = published(a_hold())

    assert payload["risk"]["state"] == "not-applicable"
    assert payload["risk"]["evaluated"] is False
    assert payload["risk"]["approved"] is False
    assert payload["risk"]["reasons"]


# ---- the guard --------------------------------------------------------

def test_publishing_without_a_decision_raises(monkeypatch):
    """A missing risk block is invisible downstream, so the publish path
    refuses rather than letting a future edit walk past it."""
    reached = []
    monkeypatch.setattr(agent, "publish", lambda *a, **k: reached.append(a))

    with pytest.raises(risk_live.RiskNotEvaluated):
        agent.publish_signal({"action": "BUY", "entry": 24_200})

    assert not reached, "the payload reached Redis despite the guard"


def test_the_guard_rejects_a_malformed_decision():
    for bad in ({"risk": None}, {"risk": "approved"}, {"risk": {}}, {}):
        with pytest.raises(risk_live.RiskNotEvaluated):
            risk_live.assert_evaluated(bad)


def test_the_guard_accepts_a_real_decision(db):
    payload = risk_live.attach(db, {"action": "BUY"}, a_buy())
    assert risk_live.assert_evaluated(payload) is payload


# ---- a cached payload from before the fix ------------------------------

def test_a_legacy_cached_signal_is_labelled_not_silently_passed():
    """The websocket opens by replaying `signal:latest`, and that blob has a
    fifteen-minute TTL — long enough to outlive the deploy that started
    attaching decisions."""
    legacy = {"action": "BUY", "entry": 24_200.0}

    marked = risk_live.ensure(legacy)

    assert marked["risk"]["state"] == "unevaluated"
    assert marked["risk"]["evaluated"] is False
    assert marked["risk"]["reasons"]
    assert legacy == {"action": "BUY", "entry": 24_200.0}, "input was mutated"


def test_ensure_never_recomputes_an_existing_decision(db):
    decided = risk_live.attach(db, {"action": "BUY"}, a_buy())
    assert risk_live.ensure(decided) is decided


def test_ensure_tolerates_no_signal_at_all():
    assert risk_live.ensure(None) is None


# ---- both routes, one decision ----------------------------------------

def test_both_routes_produce_the_same_decision(db, published, monkeypatch):
    """The point of the shared step. Same journal, same signal, same answer —
    so the socket and its fallback cannot tell the desk different things."""
    db.add_all([closed_trade(-900.0, order=1), open_trade()])
    db.commit()

    signal = a_buy()

    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=signal, plan=None))

    from_endpoint = TestClient(app).get("/signals/live").json()["risk"]
    from_socket = published(signal)["risk"]

    # Everything but the clock. The two evaluations happen microseconds
    # apart, so `evaluated_at` differs by construction — and a decision that
    # did not record when it was made is the staleness bug this file also
    # covers, so it is asserted present rather than ignored.
    assert from_socket["evaluated_at"] and from_endpoint["evaluated_at"]
    assert {k: v for k, v in from_socket.items() if k != "evaluated_at"} == \
           {k: v for k, v in from_endpoint.items() if k != "evaluated_at"}
    assert from_socket["state"] == "blocked"


def test_the_endpoint_still_carries_its_decision(db, monkeypatch):
    """H-1's fix must survive the move into the shared module."""
    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_buy(), plan=None))

    risk = TestClient(app).get("/signals/live").json()["risk"]
    assert risk["evaluated"] is True
    assert risk["day_state"]["trading_day"] == trading_date().isoformat()


# ---- no second copy of the logic --------------------------------------

def _reaches_the_risk_evaluator(path: Path) -> bool:
    """Does this module get at `risk.manager.evaluate`?

    Two earlier versions of this check were wrong in opposite directions. A
    text search for "evaluate(" also matched the word in a comment. Matching
    any call named `evaluate` then caught `outcome_study.evaluate` — the
    signal-outcome study, an entirely different function that happens to
    share a verb.

    The precise question is whether a route can reach the risk rulebook at
    all, and it cannot call what it has not imported. So: the import is the
    check, plus a qualified call through the manager module.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            # The last segment, so a relative `from .manager import evaluate`
            # counts the same as `from ..risk.manager import evaluate`.
            if node.module.split(".")[-1] == "manager" and any(
                    alias.name == "evaluate" for alias in node.names):
                return True
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "evaluate"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in {"manager", "risk_manager"}):
                return True
    return False


def test_no_live_route_evaluates_risk_for_itself():
    """Guards the shape of the fix, not just its effect.

    H-4 existed because the assembly lived inside a route. The second route
    then went without one. This fails the moment an API route or a worker
    reaches for the risk rulebook directly instead of going through the
    shared step.

    The backtest engines are deliberately not covered: they run the same
    rulebook over historical bars with their own simulated day state, which
    is a different caller with a different source of truth — not a second
    copy of the live assembly.
    """
    root = Path(__file__).resolve().parents[1] / "backend" / "app"
    live = [p for p in root.rglob("*.py")
            if p.relative_to(root).parts[0] in {"api", "workers"}]
    assert live, "test premise: found no api or worker modules"

    offenders = sorted(p.relative_to(root).as_posix() for p in live
                       if _reaches_the_risk_evaluator(p))
    assert offenders == [], offenders

    # And the shared step really is the one that reaches it.
    assert _reaches_the_risk_evaluator(root / "risk" / "live.py")


def test_the_check_can_tell_the_risk_evaluator_from_anything_else(tmp_path):
    """Guard against the guard, in both directions this check has been wrong.

    A comment mentioning it is not a call. A different module's `evaluate`
    is not the risk manager's. Both used to fail this test."""
    mentions = tmp_path / "mentions.py"
    mentions.write_text('"""We deliberately do not call evaluate() here."""\n'
                        "# evaluate(x) would be wrong\n"
                        "value = 1\n")
    other = tmp_path / "other.py"
    other.write_text("from ..evaluation import outcomes as study\n"
                     "study.evaluate(db)\n")
    real = tmp_path / "real.py"
    real.write_text("from ..risk.manager import evaluate\n"
                    "evaluate(config=1, state=2)\n")

    assert not _reaches_the_risk_evaluator(mentions)
    assert not _reaches_the_risk_evaluator(other)
    assert _reaches_the_risk_evaluator(real)


def test_the_ist_trading_day_is_what_the_cap_counts(db, published):
    """A trade from yesterday's IST session must not occupy today's cap."""
    yesterday = datetime.now(IST) - timedelta(days=1)
    stale = closed_trade(200.0)
    stale.created_at = yesterday.astimezone(UTC)
    db.add(stale)
    db.commit()

    risk = published(a_buy())["risk"]
    assert risk["day_state"]["trades_taken"] == 0
