from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..analytics import indicators, options, smc, structure
from ..cache import get_json, set_json
from ..db import get_db
from ..deps import get_broker
from ..workers import archiver

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/candles")
def candles(symbol: str = "NIFTY", interval: str = "5m", days: int = Query(5, ge=1, le=60)):
    df = indicators.enrich(get_broker().candles(symbol, interval, days))
    df = df.tail(500).copy()
    df["timestamp"] = df["timestamp"].astype(str)
    return {"symbol": symbol, "interval": interval, "candles": df.to_dict(orient="records")}


@router.get("/structure")
def market_structure(symbol: str = "NIFTY", interval: str = "5m", days: int = 5):
    df = indicators.enrich(get_broker().candles(symbol, interval, days))
    state = structure.analyse(df)
    price = float(df["close"].iloc[-1])
    gaps = smc.find_fair_value_gaps(df)
    pools = smc.find_liquidity_pools(df)
    return {
        "symbol": symbol,
        "price": price,
        "structure": state.to_dict(),
        "fair_value_gaps": [g.to_dict() for g in gaps if not g.filled][-8:],
        "order_blocks": [b.to_dict() for b in smc.find_order_blocks(df) if not b.mitigated][-8:],
        "liquidity_pools": [p.to_dict() for p in pools[:8]],
        "sweep": smc.detect_sweep(df, pools),
    }


@router.get("/option-chain")
def option_chain(symbol: str = "NIFTY", expiry: str | None = None):
    broker = get_broker()
    cached = get_json(f"chain:{symbol}:{expiry}")
    if cached:
        return cached
    try:
        chain = broker.option_chain(symbol, expiry)
        spot = broker.quote(symbol)["last_price"]
    except Exception as exc:
        raise HTTPException(502, f"could not load option chain: {exc}") from exc

    payload = {
        "symbol": symbol,
        "summary": options.summarise(chain, spot).to_dict(),
        "strikes": chain.to_dict(orient="records"),
    }
    set_json(f"chain:{symbol}:{expiry}", payload, ttl=45)
    return payload


@router.get("/vix")
def vix():
    return {"india_vix": get_broker().india_vix()}


@router.get("/archive")
def archive_coverage(symbol: str = "NIFTY", timeframe: str = "5m",
                     db: Session = Depends(get_db)):
    """How much history you have actually accumulated.

    Free sources only look back about 60 days. This number grows every day
    the agent runs, and it is what your long backtests should read from.
    """
    return archiver.coverage(db, symbol, timeframe)


@router.post("/archive/backfill")
def archive_backfill(symbol: str = "NIFTY", timeframe: str = "5m",
                     days: int = 59, db: Session = Depends(get_db)):
    """Pull the deepest window the source allows and store it.

    Run this once on day one to seed the archive, then let the agent top it
    up every five minutes.
    """
    candles = get_broker().candles(symbol, timeframe, days)
    written = archiver.archive(db, candles, symbol, timeframe)
    return {"written": written, "coverage": archiver.coverage(db, symbol, timeframe)}
