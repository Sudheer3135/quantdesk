import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..analytics import chain_greeks, indicators, options, smc, structure
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

log = logging.getLogger(__name__)

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


# Enough bars ahead of a window for its indicators to be worth reading.
# EMA200 is the longest lookback the enrichment computes, and an EMA served
# from a cold start is not a slightly-wrong EMA — it is the mean of however
# many bars happened to be loaded, which on the left edge of a scrolled
# window is whatever the user just panned into. Warm-up bars are fetched,
# enriched, and then dropped before the response.
# 1000, not a token 200. `indicators.ema` uses `adjust=False`, so an EMA is
# recursive and seeded from the first bar it is given: the seed's influence
# decays as (1-alpha)^n, and at alpha = 2/201 that is still ~8% after 250
# bars. The visible symptom is the same bar reading differently depending on
# which way the user scrolled onto it. A thousand bars puts the residue
# under a hundredth of a point, which is far below a tick. It is asymptotic,
# never exactly zero — this is the number at which that stops mattering.
HISTORY_WARMUP_BARS = 1000

# The ceiling on one request. Deep enough that panning left feels
# continuous rather than paged, bounded so a client cannot ask for the
# whole archive in one query and enrich it on the request thread.
HISTORY_MAX_BARS = 1500


@router.get("/candles/history")
def candles_history(
    symbol: str = "NIFTY",
    interval: str = "5m",
    before: str | None = None,
    limit: int = Query(500, ge=1, le=HISTORY_MAX_BARS),
    db: Session = Depends(get_db),
):
    """A window of archived candles, for a chart being panned backwards.

    `/market/candles` answers "what is happening now" from the broker and is
    capped accordingly. This answers "what happened before this bar" from
    the archive, so a chart can keep loading older data as the user scrolls
    without ever asking the broker for years it does not serve.

    `before` is an exclusive ISO timestamp cursor: pass the oldest bar you
    already hold and you get the window immediately preceding it, with no
    duplicate at the seam. Omit it for the most recent window.

    Rows come back oldest-first, the same order as `/market/candles`.
    """
    symbol = validate_symbol(symbol)

    cursor: datetime | None = None
    if before:
        try:
            cursor = datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="`before` must be an ISO 8601 timestamp") from None

    # Read backwards from the cursor, then flip. The archive is indexed
    # oldest-first, but a chart panning left wants the *newest* rows below
    # the cursor, and taking `limit` from the front of the whole history
    # would hand it 2026-05-15 every time.
    want = limit + HISTORY_WARMUP_BARS

    # `newest=True` pushes the limit into SQL. Without it this loads every
    # bar from the start of the archive up to the cursor and throws away all
    # but the last `want` — fine against 5,000 rows, and O(all history) per
    # pan once a multi-year backfill lands.
    #
    # One extra row is asked for so the exclusive cursor below cannot leave
    # the page short, and so `has_more` still has something to compare.
    df = repository.load_index_candles(
        db, symbol=symbol, timeframe=interval, end=cursor,
        limit=want + 1, newest=True)
    if cursor is not None and not df.empty:
        # `end` is inclusive in the repository, and the cursor is a bar the
        # caller already has.
        df = df[df["timestamp"] < cursor]
    fetched = len(df)
    df = df.tail(want)

    # Enriching an empty frame raises: the columns come back as object dtype
    # and the indicator maths has no cumsum for that. Reaching the start of
    # the archive is a normal thing for a chart being panned, not a fault,
    # so it answers empty rather than 500-ing.
    window = indicators.enrich(df).tail(limit) if not df.empty else df

    return {
        "symbol": symbol,
        "interval": interval,
        "candles": jsonable_records(window),
        # Whether panning further left is worth a request. False stops the
        # chart asking the same empty question every time it hits the start
        # of the archive.
        #
        # Measured against what the database had available, not against the
        # trimmed window: the warm-up bars are real older rows, and a page
        # that filled its request is evidence there is more behind it.
        "has_more": fetched > len(window),
        "oldest": (window["timestamp"].iloc[0].isoformat()
                   if not window.empty else None),
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


def _chain_spot(frame) -> float:
    """The index price to read a streamed chain against.

    This used to be the median strike of the band — and the band is centred
    on the spot at the moment the universe was built, then only re-centred
    once price has drifted five strikes (250 points) away. So the "spot"
    handed to `summarise` could sit 250 points from the market. On 31-Aug
    the desk showed ATM 24,150 with NIFTY at 24,022, and because `summarise`
    scores spot against max pain, the chain's bullish/bearish reading could
    flip on that error alone.

    The live price is the same Angel socket the chain arrives on, so using
    it keeps the two feeds reading one moment. The median strike remains as
    a last resort, for a chain that streams before any price has published.
    """
    latest = get_json("price:latest") or {}
    try:
        spot = float(latest.get("price"))
    except (TypeError, ValueError):
        spot = float("nan")
    if spot > 0:
        return spot
    return float(frame["strike"].median())


def live_chain() -> dict | None:
    """The streamed chain, or None if it cannot answer right now.

    Returns None rather than an empty chain on every failure path. A caller
    that gets None falls back to the poll and still has a chain; a caller
    handed an empty one would think the market had no options in it.
    """
    if not get_settings().angel_options_enabled:
        return None
    try:
        from ..workers.option_chain_live import CHAIN
        snap = CHAIN.snapshot()
        frame = snap["frame"]
        if len(frame) < 3:
            return None
        # Far strikes may be quiet for minutes, but a whole chain with no
        # fresh quote has stopped streaming. Let HTTP use its polled source
        # even when the index feed and browser websocket remain healthy.
        newest_age = snap.get("newest_age_seconds")
        if newest_age is not None and newest_age > 15:
            return None
        price = _chain_spot(frame)
        return {
            "symbol": get_settings().watch_symbol,
            "summary": options.summarise(frame, price).to_dict(),
            # Angel's stream carries no IV and no greeks, so they are
            # derived here rather than left blank — see chain_greeks.
            "strikes": chain_greeks.enrich(
                jsonable_records(frame), price, snap["expiry"]),
            "fetched_at": snap["at"],
            "live": True,
            "transport": "stream",
            "expiry": snap["expiry"],
            # The chain is a composite of prints of different ages, so the
            # oldest one is the honest headline number rather than `at`.
            "oldest_age_seconds": snap["oldest_age_seconds"],
            "contracts": snap["contracts"],
            "dropped_stale": snap["dropped_stale"],
        }
    except Exception as exc:                              # noqa: BLE001
        log.warning("live option chain unavailable, falling back: %s", exc)
        return None


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

    # The streamed chain first, when there is one. It is roughly four
    # hundred milliseconds behind the exchange against the poll's sixty
    # seconds, and it costs NSE nothing. Falls through to the polled path
    # whenever it is switched off, still filling, or gone quiet — this is a
    # preference, not a dependency.
    live = live_chain()
    if live is not None:
        return live

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
        # NSE states its own IV, which is believed; the greeks are
        # derived from it so both transports carry the same columns.
        "strikes": chain_greeks.enrich(
            jsonable_records(chain), spot,
            expiry or chain.attrs.get("expiry")),
        "fetched_at": utc_now().isoformat(),
        "live": live,
        # Stated on this path too. Without it the dashboard read "Expiry
        # unstated" whenever the stream fell back — the moment you most
        # want to know which series you are looking at.
        "expiry": expiry or chain.attrs.get("expiry"),
        "transport": "poll",
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
    """India VIX — the Angel stream when it is fresh, NSE's poll otherwise.

    v2's own risk gate has read the streamed VIX from day one and gets it
    sub-second fresh; this endpoint, which is what the dashboard tile
    actually shows, always polled NSE directly instead — on every request,
    with no cache — the same staleness the price had before Angel was
    added, measured elsewhere in this codebase at refreshing roughly once
    a minute. Preferring the stream here is the same fix, applied to the
    one place it was missed.
    """
    from ..workers import vix_live
    live = vix_live.VIX.current()
    if live is not None:
        return {"india_vix": live, "source": "angel", "transport": "stream"}
    value = get_broker().india_vix()
    return {"india_vix": value, "source": get_settings().broker, "transport": "poll"}


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
