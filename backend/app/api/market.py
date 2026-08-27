from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..analytics import indicators, options, smc, structure
from ..brokers.base import UnknownSymbol
from ..cache import get_json, set_json
from ..config import get_settings
from ..data import importer, repository
from ..db import get_db
from ..deps import get_broker
from ..market_hours import is_open as market_is_open
from ..models import utc_now
from ..security import require_api_key
from ..symbols import validate as validate_symbol
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
    symbol = validate_symbol(symbol)
    df = indicators.enrich(get_broker().candles(symbol, interval, days))
    return {
        "symbol": symbol,
        "interval": interval,
        "candles": jsonable_records(df.tail(500)),
    }


@router.get("/structure")
def market_structure(symbol: str = "NIFTY", interval: str = "5m", days: int = 5):
    symbol = validate_symbol(symbol)
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


# Longer than the dashboard's sixty-second refresh, and deliberately so. At
# the previous forty-five the cache expired a quarter of a minute before
# every poll, so it never once served one: an open browser tab was a
# standing order for one live NSE call per minute, forever.
CHAIN_TTL_SECONDS = 120

# The last chain fetched successfully, kept long enough to answer through a
# closed market and an overnight. This is what an out-of-hours request is
# served from, and it is what makes the market-hours gate below free rather
# than a refusal.
CHAIN_LAST_TTL_SECONDS = 24 * 3600


@router.get("/option-chain")
def option_chain(symbol: str = "NIFTY", expiry: str | None = None):
    """The current option chain, or the last one when the market is shut.

    This endpoint and the option collector share one throttled NSE session,
    and they are not equally important: a dashboard request can be answered
    from cache, while a snapshot the collector misses cannot be re-collected
    at any price. So the browser is not allowed to spend the collector's
    budget. A live fetch happens only while the market is open; outside those
    hours the last good chain is served instead, labelled `live: false`
    rather than passed off as current.

    Measured across one retained log before this gate existed: 662 of 1,272
    upstream NSE calls — 52% — were made after the close, by a browser tab
    nobody had closed.
    """
    symbol = validate_symbol(symbol)
    key = f"chain:{symbol}:{expiry}"
    last_key = f"chain:last:{symbol}:{expiry}"

    cached = get_json(key)
    if cached:
        return cached

    # A closed market has nothing new to say. Serving the last chain costs
    # no upstream call and leaves the NSE session rested for the next open.
    #
    # When there is no last chain the request falls through and fetches one,
    # so a dashboard opened out of hours is not blank. That costs at most one
    # call per CHAIN_LAST_TTL_SECONDS, because the answer is then cached for
    # a day — not the one-per-minute standing order this gate replaced.
    live = market_is_open()
    if not live:
        stale = get_json(last_key)
        if stale:
            return {**stale, "live": False}

    broker = get_broker()
    try:
        chain = broker.option_chain(symbol, expiry)
        spot = broker.quote(symbol)["last_price"]
    except UnknownSymbol:
        # The caller named something we do not carry. Let it reach the 422
        # handler instead of being relabelled as an upstream failure.
        raise
    except Exception as exc:
        raise HTTPException(502, f"could not load option chain: {exc}") from exc

    payload = {
        "symbol": symbol,
        "summary": options.summarise(chain, spot).to_dict(),
        # Same guard as the candle endpoint. NSE returns no IV for untraded
        # strikes, and a chain wide enough to include them would otherwise
        # 500 on the same NaN.
        "strikes": jsonable_records(chain),
        "fetched_at": utc_now().isoformat(),
        "live": live,
    }
    # The short cache only exists to absorb an open market's repeat polls;
    # out of hours the day-long key is the one that must answer, and writing
    # the short one too would just expire and let another call through.
    if live:
        set_json(key, payload, ttl=CHAIN_TTL_SECONDS)
    set_json(last_key, payload, ttl=CHAIN_LAST_TTL_SECONDS)
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


@router.post("/archive/backfill", deprecated=True, dependencies=[Depends(require_api_key)])
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
