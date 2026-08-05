from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import signal_engine
from ..config import get_settings
from ..db import get_db
from ..deps import get_broker
from ..models import SignalRecord
from ..risk.manager import DayState, RiskConfig, evaluate

router = APIRouter(prefix="/signals", tags=["signals"])


def build_signal(symbol: str, timeframe: str, days: int = 5) -> signal_engine.Signal:
    broker = get_broker()
    candles = broker.candles(symbol, timeframe, days)
    try:
        chain = broker.option_chain(symbol)
    except Exception:
        chain = None
    return signal_engine.generate(
        candles, symbol=symbol, timeframe=timeframe,
        chain=chain, india_vix=broker.india_vix(),
    )


@router.get("/live")
def live_signal(symbol: str = "NIFTY", timeframe: str = "5m",
                persist: bool = False, db: Session = Depends(get_db)):
    try:
        sig = build_signal(symbol, timeframe)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    payload = sig.to_dict()
    payload["explanation"] = sig.explain()

    if sig.action != "HOLD":
        s = get_settings()
        decision = evaluate(
            config=RiskConfig(
                capital=s.capital,
                risk_per_trade_pct=s.risk_per_trade_pct,
                max_trades_per_day=s.max_trades_per_day,
                min_risk_reward=s.min_risk_reward,
                lot_size=s.lot_size,
            ),
            state=DayState(trading_day=__import__("datetime").date.today()),
            entry=sig.entry, stop_loss=sig.stop_loss, target=sig.target,
        )
        payload["risk"] = decision.to_dict()

    if persist:
        record = SignalRecord(
            symbol=sig.symbol, timeframe=sig.timeframe, action=sig.action,
            confidence=sig.confidence, price=sig.price, entry=sig.entry,
            stop_loss=sig.stop_loss, target=sig.target,
            checks=[c.to_dict() for c in sig.checks], context=sig.context,
        )
        db.add(record)
        db.commit()
        payload["id"] = record.id

    return payload


@router.get("/history")
def signal_history(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(SignalRecord).order_by(SignalRecord.created_at.desc()).limit(limit)
    ).all()
    return [
        {"id": r.id, "created_at": r.created_at, "symbol": r.symbol, "action": r.action,
         "confidence": r.confidence, "price": r.price, "entry": r.entry,
         "stop_loss": r.stop_loss, "target": r.target}
        for r in rows
    ]
