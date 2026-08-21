"""Trade journal — log trades, then review them honestly after the close."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import TradeRecord
from ..security import require_api_key

router = APIRouter(prefix="/journal", tags=["journal"])


class TradeIn(BaseModel):
    symbol: str
    side: str = Field(pattern="^(BUY|SELL)$")
    quantity: int = Field(gt=0)
    entry: float
    stop_loss: float
    target: float | None = None
    setup: str | None = None
    signal_id: int | None = None


class TradeClose(BaseModel):
    exit: float
    plan_followed: bool | None = None
    mistakes: str | None = None
    lesson: str | None = None
    score: int | None = Field(default=None, ge=1, le=100)


@router.post("", dependencies=[Depends(require_api_key)])
def open_trade(payload: TradeIn, db: Session = Depends(get_db)):
    trade = TradeRecord(**payload.model_dump(), status="open")
    db.add(trade)
    db.commit()
    return {"id": trade.id, "status": trade.status}


@router.post("/{trade_id}/close", dependencies=[Depends(require_api_key)])
def close_trade(trade_id: int, payload: TradeClose, db: Session = Depends(get_db)):
    trade = db.get(TradeRecord, trade_id)
    if not trade:
        raise HTTPException(404, "trade not found")
    if trade.status == "closed":
        raise HTTPException(409, "trade is already closed")

    direction = 1 if trade.side == "BUY" else -1
    trade.exit = payload.exit
    trade.pnl = (payload.exit - trade.entry) * direction * trade.quantity
    risk = abs(trade.entry - trade.stop_loss) * trade.quantity
    trade.r_multiple = round(trade.pnl / risk, 3) if risk else None
    trade.status = "closed"
    for field in ("plan_followed", "mistakes", "lesson", "score"):
        value = getattr(payload, field)
        if value is not None:
            setattr(trade, field, value)
    db.commit()
    return {"id": trade.id, "pnl": trade.pnl, "r_multiple": trade.r_multiple}


@router.get("")
def list_trades(status: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    stmt = select(TradeRecord).order_by(TradeRecord.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(TradeRecord.status == status)
    return db.scalars(stmt).all()


@router.get("/stats")
def journal_stats(db: Session = Depends(get_db)):
    closed = db.scalars(select(TradeRecord).where(TradeRecord.status == "closed")).all()
    if not closed:
        return {"trades": 0, "note": "No closed trades logged yet."}
    pnls = [t.pnl or 0 for t in closed]
    wins = [p for p in pnls if p > 0]
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    followed = [t for t in closed if t.plan_followed is True]
    return {
        "trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2),
        "net_pnl": round(sum(pnls), 2),
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "plan_adherence_pct": round(len(followed) / len(closed) * 100, 2),
        "avg_self_score": round(
            sum(t.score for t in closed if t.score) / max(1, len([t for t in closed if t.score])), 1
        ),
    }
