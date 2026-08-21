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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics.signal_engine import Signal
from app.api import signals as signals_api
from app.config import get_settings
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


def closed_trade(pnl, minutes_ago=30):
    return TradeRecord(
        symbol="NIFTY", side="BUY", quantity=75,
        entry=24_200.0, stop_loss=24_190.0, status="closed", pnl=pnl,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
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
        monkeypatch.setattr(agent, "build_signal", lambda *a, **k: signal)
        agent.tick()
        assert sent, "the agent published nothing"
        return sent["payload"]

    return run


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
    db.add_all([closed_trade(500.0), closed_trade(300.0)])   # cap is 2
    db.commit()

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
    db.add_all([closed_trade(-800.0, minutes_ago=60),
                closed_trade(-700.0, minutes_ago=30)])
    db.commit()

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

    db.add(closed_trade(-4_000.0))          # limit is 3% of 100,000
    db.commit()

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
    db.add_all([closed_trade(-900.0), open_trade()])
    db.commit()

    signal = a_buy()

    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(signals_api, "build_signal", lambda *a, **k: signal)

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
    monkeypatch.setattr(signals_api, "build_signal", lambda *a, **k: a_buy())

    risk = TestClient(app).get("/signals/live").json()["risk"]
    assert risk["evaluated"] is True
    assert risk["day_state"]["trading_day"] == trading_date().isoformat()


# ---- no second copy of the logic --------------------------------------

def _calls_named(path: Path, name: str) -> bool:
    """Does this module actually *call* `name`, per the parse tree?

    The first version of this test searched the source text for "evaluate(",
    which also matches the word inside a comment or a docstring — so a future
    note explaining why a route does not evaluate risk would have failed it.
    An AST walk asks the question that was meant: is there a call here.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False


def test_no_live_route_evaluates_risk_for_itself():
    """Guards the shape of the fix, not just its effect.

    H-4 existed because the assembly lived inside a route. The second route
    then went without one. This fails the moment an API route or a worker
    calls `evaluate` directly again instead of going through the shared step.

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
                       if _calls_named(p, "evaluate"))
    assert offenders == [], offenders

    # And the shared step really is the one that calls it.
    assert _calls_named(root / "risk" / "live.py", "evaluate")


def test_the_ast_check_can_tell_a_call_from_a_mention(tmp_path):
    """Guard against the guard. A checker that never finds a call would make
    the assertion above pass vacuously, and one that matched text would fail
    on a comment."""
    mentions = tmp_path / "mentions.py"
    mentions.write_text('"""We deliberately do not call evaluate() here."""\n'
                        "# evaluate(x) would be wrong\n"
                        "value = 1\n")
    calls = tmp_path / "calls.py"
    calls.write_text("from x import evaluate\nevaluate(1)\n")

    assert not _calls_named(mentions, "evaluate")
    assert _calls_named(calls, "evaluate")


def test_the_ist_trading_day_is_what_the_cap_counts(db, published):
    """A trade from yesterday's IST session must not occupy today's cap."""
    yesterday = datetime.now(IST) - timedelta(days=1)
    stale = closed_trade(200.0)
    stale.created_at = yesterday.astimezone(UTC)
    db.add(stale)
    db.commit()

    risk = published(a_buy())["risk"]
    assert risk["day_state"]["trades_taken"] == 0
