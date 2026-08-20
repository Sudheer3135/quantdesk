from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..analytics import indicators, options, smc, structure
from ..cache import get_json, set_json
from ..config import get_settings
from ..data import importer, repository
from ..db import get_db
from ..deps import get_broker
from .serialization import jsonable_records

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/candles")
def candles(symbol: str = "NIFTY", interval: str = "5m", days: int = Query(5, ge=1, le=60)):
    """Enriched candles.

    Indicator columns are null wherever the reading is unavailable — most
    often `rvol`, which is NaN for the whole series when the source
    publishes no volume. Null is the honest answer: substituting a neutral
    number would be indistinguishable from a real neutral reading.
    """
    df = indicators.enrich(get_broker().candles(symbol, interval, days))
    return {
        "symbol": symbol,
        "interval": interval,
        "candles": jsonable_records(df.tail(500)),
    }


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
        # Same guard as the candle endpoint. NSE returns no IV for untraded
        # strikes, and a chain wide enough to include them would otherwise
        # 500 on the same NaN.
        "strikes": jsonable_records(chain),
    }
    set_json(f"chain:{symbol}:{expiry}", payload, ttl=45)
    return payload


@router.get("/vix")
def vix():
    return {"india_vix": get_broker().india_vix()}


@router.get("/archive", deprecated=True)
def archive_coverage(symbol: str = "NIFTY", timeframe: str = "5m",
                     db: Session = Depends(get_db)):
    """Deprecated alias for GET /data/coverage.

    Kept so existing scripts and bookmarks keep working; stored history
    lives under /data now.
    """
    return repository.coverage(db, symbol, timeframe).to_dict()


@router.post("/archive/backfill", deprecated=True)
def archive_backfill(symbol: str = "NIFTY", timeframe: str = "5m",
                     days: int = 59, db: Session = Depends(get_db)):
    """Deprecated alias for POST /data/import/index.

    Routed through the same importer rather than kept as a second write
    path. Two ways of writing one table means two sets of guards to keep in
    step, and the one that gets forgotten is the one that corrupts the
    archive.
    """
    candles = get_broker().candles(symbol, timeframe, days)
    report = importer.import_index_candles(
        db, candles, symbol, timeframe, source=get_settings().broker)
    return {
        "written": report.write.written,
        "import": report.to_dict(),
        "coverage": repository.coverage(db, symbol, timeframe).to_dict(),
    }
