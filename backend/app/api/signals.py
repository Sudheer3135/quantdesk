from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics import signal_engine
from ..brokers.base import UnknownSymbol
from ..db import get_db
from ..deps import get_broker
from ..models import SignalRecord
from ..risk import live as risk_live
from ..security import HEADER as API_KEY_HEADER
from ..security import key_is_valid
from ..symbols import validate as validate_symbol

router = APIRouter(prefix="/signals", tags=["signals"])


def build_signal(symbol: str, timeframe: str, days: int = 5) -> signal_engine.Signal:
    symbol = validate_symbol(symbol)
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
                persist: bool = False, db: Session = Depends(get_db),
                x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)):
    # Reading a signal is open; asking for it to be *stored* is not. This is
    # a GET that writes a row, so it needs the same key as the POST routes —
    # guarding by HTTP verb alone would have left this one open.
    if persist and not key_is_valid(x_api_key):
        raise HTTPException(
            401, f"persist=true writes a signal row. Send your key in the {API_KEY_HEADER} header.")
    try:
        sig = build_signal(symbol, timeframe)
    except UnknownSymbol:
        # The caller named something we do not carry. Let it reach the 422
        # handler instead of being relabelled as an upstream failure.
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    payload = sig.to_dict()
    payload["explanation"] = sig.explain()

    # The risk decision is assembled in `risk.live` and nowhere else. It used
    # to be built inline here, which is how the agent's route — the one the
    # dashboard actually reads — ended up publishing signals with no risk
    # block at all. See audit finding H-4.
    risk_live.attach(db, payload, sig)

    if persist:
        record = SignalRecord(
            symbol=sig.symbol, timeframe=sig.timeframe, action=sig.action,
            confidence=sig.confidence, price=sig.price, entry=sig.entry,
            stop_loss=sig.stop_loss, target=sig.target,
            checks=[c.to_dict() for c in sig.checks], context=sig.context,
            # Stored so "what did the desk decide about this signal, and
            # why" is answerable from the database rather than only from
            # whatever was on screen at the time.
            risk=payload["risk"],
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
