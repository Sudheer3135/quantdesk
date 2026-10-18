"""The one place a live signal gets its risk decision.

Audit finding H-4. Signals reach the dashboard by two routes: the agent
publishes to Redis and the websocket relays that, while `/signals/live` is a
sixty-second fallback used only when the socket is down. The risk block was
built inline in the endpoint, so the fallback carried a decision and the
primary route carried none at all — `evaluate()` was never called on it.

Nothing about that was visible. `PlanPanel` guards its risk rows with
`{risk && ...}`, so a missing block does not render an error; it renders a
clean trade plan with entry, stop and target and no mention that the trade
was refused.

This module owns no rules. Every threshold still lives in `manager.evaluate`
and every limit still comes from `deps.risk_config`. What it owns is the
assembly, and two distinctions the first version of it got wrong:

  Actual versus potential exposure. A refused trade risks nothing, but
  `evaluate` computes the sizing before the refusal accumulates, so the
  payload reported a blocked trade as having a thousand rupees at risk. The
  number is worth keeping — it is how you see what the trade would have
  been — but it is not exposure, and the dashboard was showing it as one.

  Then versus now. A decision is made when the signal is published and the
  journal moves underneath it: take a position at 10:01 and the 10:00
  verdict is stale by 10:02, but a reconnecting browser would replay it as
  current. So a decision carries the instant it was made, and `current()`
  re-runs the same assembly against today's journal for the state now.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..data import repository
from ..deps import risk_config
from ..market_hours import trading_date
from .manager import day_state_from_trades, evaluate

# What the dashboard renders as a badge. Four states rather than a boolean:
# "this signal proposes no trade" is not the same answer as "this trade was
# refused", and a HOLD shown as BLOCKED would misreport a quiet market as a
# risk veto.
APPROVED = "approved"
BLOCKED = "blocked"
NOT_APPLICABLE = "not-applicable"
UNEVALUATED = "unevaluated"

NO_TRADE_REASON = "No trade proposed — nothing to size."
UNEVALUATED_REASON = (
    "This signal was published without a risk decision. It will carry one "
    "from the next agent tick."
)


def _block(state: str, *, evaluated: bool, approved: bool, reasons: list[str],
           quantity: int = 0, lots: int = 0, rupees_at_risk: float = 0.0,
           potential: dict | None = None, risk_per_unit: float = 0.0,
           risk_reward: float | None = None, day_state: dict | None = None,
           evaluated_at: str | None = None) -> dict:
    """The shape the dashboard reads, built in exactly one place.

    `quantity`, `lots` and `rupees_at_risk` are *actual* — what this decision
    permits, which is nothing at all unless it was approved. What the sizing
    arithmetic produced lives under `potential`, where it explains the
    decision without claiming to be exposure.
    """
    return {
        "state": state,
        "evaluated": evaluated,
        "evaluated_at": evaluated_at,
        "approved": approved,
        "reasons": reasons,
        "quantity": quantity,
        "lots": lots,
        # `rupees_at_risk` is the name this number goes by everywhere a human
        # reads it. `risk_amount` is what `RiskDecision` calls it and what
        # existing consumers already read, so both are published rather than
        # renaming a field across a boundary for cosmetic reasons.
        "rupees_at_risk": rupees_at_risk,
        "risk_amount": rupees_at_risk,
        "potential": potential or {"quantity": 0, "lots": 0, "rupees_at_risk": 0.0},
        "risk_per_unit": risk_per_unit,
        "risk_reward": risk_reward,
        "day_state": day_state,
    }


def _decide(db: Session, action: str | None, entry, stop_loss, target) -> dict:
    """Judge one proposed trade against today's journal.

    Every caller lands here, so the socket, the endpoint and the "risk now"
    recomputation cannot answer the same question differently.
    """
    if action == "HOLD" or entry is None or stop_loss is None or target is None:
        return _block(NOT_APPLICABLE, evaluated=False, approved=False,
                      reasons=[NO_TRADE_REASON])

    day = trading_date()
    state = day_state_from_trades(
        trading_day=day,
        todays=repository.todays_trades(db, day),
        open_now=repository.open_trades(db),
        closed_today=repository.closed_trades(db, day),
    )
    decision = evaluate(
        config=risk_config(),
        state=state,
        entry=float(entry), stop_loss=float(stop_loss), target=float(target),
    )

    potential = {
        "quantity": decision.quantity,
        "lots": decision.lots,
        "rupees_at_risk": decision.risk_amount,
    }

    return _block(
        APPROVED if decision.approved else BLOCKED,
        evaluated=True,
        evaluated_at=datetime.now(UTC).isoformat(),
        approved=decision.approved,
        reasons=decision.reasons,
        # A refused trade has no position and no money on the table.
        quantity=decision.quantity if decision.approved else 0,
        lots=decision.lots if decision.approved else 0,
        rupees_at_risk=decision.risk_amount if decision.approved else 0.0,
        potential=potential,
        risk_per_unit=decision.risk_per_unit,
        risk_reward=decision.risk_reward,
        day_state={
            "trading_day": day.isoformat(),
            "trades_taken": state.trades_taken,
            "realised_pnl": state.realised_pnl,
            "consecutive_losses": state.consecutive_losses,
            "open_positions": state.open_positions,
        },
    )


def decide(db: Session, sig) -> dict:
    """The decision for a freshly built `Signal`."""
    return _decide(db, sig.action, sig.entry, sig.stop_loss, sig.target)


def current(db: Session, payload: dict | None) -> dict | None:
    """Re-judge a published signal against the journal as it stands now.

    The levels are the signal's; the day state is today's. This is what the
    desk needs before acting on a plan that was published minutes ago — the
    signal has not changed, but the number of positions you are holding may
    have.

    Deliberately the same `_decide` the original verdict came from. A second
    implementation of "what would risk say" is how the two answers start
    disagreeing, which is the whole shape of H-4.
    """
    if not isinstance(payload, dict):
        return None
    return _decide(db, payload.get("action"), payload.get("entry"),
                   payload.get("stop_loss"), payload.get("target"))


def attach(db: Session, payload: dict, sig) -> dict:
    """Put the decision on the payload. Both publish routes call this."""
    payload["risk"] = decide(db, sig)
    return payload


def ensure(payload: dict | None) -> dict | None:
    """Give a payload a risk block if it somehow has none.

    For the read side, where raising helps nobody. The websocket opens by
    replaying the last signal out of Redis, and that blob can outlive a
    deploy — a cache written before this fix has no risk key and a
    fifteen-minute TTL. Refusing to show it would blank the dashboard;
    showing it unmarked would reproduce exactly the silence H-4 was about.
    So it is shown, labelled as what it is.

    Never recomputes. A payload that already has a decision is returned
    untouched — re-deciding here would put a second evaluation on a second
    route.
    """
    if not isinstance(payload, dict):
        return payload
    risk = payload.get("risk")
    if isinstance(risk, dict) and "state" in risk:
        return payload
    return payload | {"risk": _block(UNEVALUATED, evaluated=False,
                                     approved=False,
                                     reasons=[UNEVALUATED_REASON])}


class RiskNotEvaluated(RuntimeError):
    """A signal payload reached a publish path without a risk decision."""


def assert_evaluated(payload: dict) -> dict:
    """Refuse to hand on a signal that never went through `attach`.

    The guard exists because the failure it catches is silent. Dropping the
    risk block breaks no test that only checks the fields it does read, and
    the dashboard renders the result without complaint — the trade plan just
    quietly loses the line saying it was refused. A publish path that raises
    is one a future edit cannot walk past.
    """
    risk = payload.get("risk")
    if not isinstance(risk, dict) or "state" not in risk:
        raise RiskNotEvaluated(
            "signal payload has no risk decision. Every live signal must go "
            "through risk.live.attach() before it is published — see audit "
            "finding H-4."
        )
    return payload
