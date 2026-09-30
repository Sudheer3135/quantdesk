"""Strategy v2 on paper: its account, its positions, and every decision.

Read endpoints answer from the database and the running trader. The one
write, closing the open paper position by hand, sits behind the API key like
every other write on the desk.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PaperDecision, PaperPosition
from ..security import require_api_key
from ..strategy_v2 import paper
from ..strategy_v2 import vix as vix_history
from ..strategy_v2.config import NAME

router = APIRouter(prefix="/v2", tags=["strategy-v2"])


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """The simulated account, the open position, and every gate right now."""
    return paper.TRADER.status(db=db)


@router.get("/positions")
def positions(status: str | None = Query(default=None, pattern="^(open|closed)$"),
              limit: int = Query(default=50, ge=1, le=500),
              db: Session = Depends(get_db)):
    stmt = (select(PaperPosition).where(PaperPosition.strategy == NAME)
            .order_by(PaperPosition.id.desc()).limit(limit))
    if status:
        stmt = stmt.where(PaperPosition.status == status)
    rows = db.scalars(stmt).all()
    closed = [r for r in rows if r.status == "closed" and r.pnl is not None]
    wins = [r for r in closed if r.pnl > 0]
    return {
        "positions": [paper.position_dict(r) for r in rows],
        "summary": {
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
            "net_pnl": round(sum(r.pnl for r in closed), 2),
            "avg_r": (round(sum(r.r_multiple or 0 for r in closed) / len(closed), 3)
                      if closed else None),
        },
    }


@router.get("/decisions")
def decisions(day: date | None = None, limit: int = Query(default=200, ge=1, le=2000),
              db: Session = Depends(get_db)):
    stmt = (select(PaperDecision).where(PaperDecision.strategy == NAME)
            .order_by(PaperDecision.id.desc()).limit(limit))
    if day:
        stmt = stmt.where(PaperDecision.session_date == day)
    return {"decisions": [{
        "id": d.id, "decided_at": d.decided_at.isoformat() if d.decided_at else None,
        "session_date": d.session_date.isoformat(), "signal_time": d.signal_time,
        "action": d.action, "outcome": d.outcome, "code": d.code,
        # The evidence envelope is served on its own below; the list keeps
        # the shape it always had.
        "detail": {k: v for k, v in (d.detail or {}).items() if k != "evidence"},
        "evidence_status": ((d.detail or {}).get("evidence") or {}).get("status",
                                                                        "not_recorded"),
    } for d in db.scalars(stmt).all()]}


@router.get("/decisions/{decision_id}/evidence")
def decision_evidence(decision_id: int, db: Session = Depends(get_db)):
    """What v2 saw when it made this decision (Phase 3B). Decisions filed
    before evidence existed say so rather than returning an empty record."""
    d = db.get(PaperDecision, decision_id)
    if d is None or d.strategy != NAME:
        raise HTTPException(status_code=404, detail="no such v2 decision")
    envelope = (d.detail or {}).get("evidence")
    return {"id": d.id, "outcome": d.outcome, "code": d.code,
            "evidence": envelope if envelope is not None
            else {"status": "not_recorded",
                  "note": "filed before decision evidence was captured"}}


@router.get("/vix")
def vix(db: Session = Depends(get_db)):
    return {"history": vix_history.coverage(db)}


@router.post("/close", dependencies=[Depends(require_api_key)])
def close():
    """Close the open paper position at the live bid."""
    closed = paper.TRADER.close_now()
    if closed is None:
        raise HTTPException(status_code=404, detail="no open v2 paper position")
    return closed
