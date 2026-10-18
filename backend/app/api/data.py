"""Historical data endpoints.

Importing, inspecting and versioning the dataset. Deliberately separate from
`/market`, which serves live readings: these operate on stored history, and
conflating "what is the market doing" with "what do I have on disk" is how
a backtest ends up reading a live feed.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..brokers.base import UnknownSymbol
from ..config import get_settings
from ..data import dataset as dataset_module
from ..data import importer, quality, readiness, regime_store, repository
from ..db import get_db
from ..deps import get_broker
from ..security import require_api_key

log = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])


@router.post("/import/index", dependencies=[Depends(require_api_key)])
def import_index(
    symbol: str = "NIFTY",
    timeframe: str = "5m",
    days: int = Query(59, ge=1, le=3650),
    db: Session = Depends(get_db),
):
    """Pull the deepest window the source allows and store it.

    Run once to seed the archive, then let the agent top it up every five
    minutes. Safe to re-run: overlapping windows converge rather than
    duplicate.

    The free path caps intraday history at about 59 days. Asking for more
    does not fail — the source simply returns less — which is why the
    response reports what was actually stored rather than what was asked
    for.
    """
    settings = get_settings()
    try:
        candles = get_broker().candles(symbol, timeframe, days)
    except UnknownSymbol:
        # The caller named something we do not carry. Let it reach the 422
        # handler instead of being relabelled as an upstream failure.
        raise
    except Exception as exc:
        raise HTTPException(502, f"could not load candles: {exc}") from exc

    report = importer.import_index_candles(
        db, candles, symbol, timeframe, source=settings.broker)
    return {
        "import": report.to_dict(),
        "coverage": repository.coverage(db, symbol, timeframe).to_dict(),
    }


@router.post("/import/options", dependencies=[Depends(require_api_key)])
def import_options(
    symbol: str = "NIFTY",
    expiry: str | None = None,
    timeframe: str = "5m",
    db: Session = Depends(get_db),
):
    """Capture one option-chain snapshot into the archive.

    This is the only way option history exists at all. NSE publishes a live
    snapshot rather than a tape, and no free source sells the history, so
    the archive fills forward one poll at a time from the day you start.

    Nothing here can be backfilled. A backtest wanting real premiums has to
    wait for the data to accumulate, and until then the option engine prices
    with Black-Scholes and says so in its `assumptions` block.
    """
    if expiry is None:
        # Checked before anything is fetched. `parse_option_chain` returns
        # the nearest expiry when none is asked for but does not say which
        # one that was, so a snapshot filed under a guess silently merges
        # two different contracts — refuse rather than guess.
        #
        # The order matters beyond tidiness. This ran *after* the chain
        # fetch, so a request that was always going to be refused first made
        # a live call to NSE. That wasted a request against an endpoint this
        # platform already has to throttle, and it made the refusal
        # conditional on the network: when NSE was slow or rate-limiting,
        # the caller got a 502 about a fetch that never needed to happen
        # instead of the 400 explaining what they did wrong.
        raise HTTPException(
            400,
            "expiry is required. The chain endpoint defaults to the nearest "
            "expiry without reporting which, and a snapshot filed under the "
            "wrong expiry silently merges two different contracts. Read the "
            "list from GET /market/option-chain first.")

    settings = get_settings()
    broker = get_broker()
    try:
        if hasattr(broker, "chain_with_spot"):
            chain, spot = broker.chain_with_spot(symbol, expiry)
        else:
            chain = broker.option_chain(symbol, expiry)
            spot = broker.quote(symbol)["last_price"]
    except UnknownSymbol:
        # The caller named something we do not carry. Let it reach the 422
        # handler instead of being relabelled as an upstream failure.
        raise
    except Exception as exc:
        raise HTTPException(502, f"could not load option chain: {exc}") from exc

    try:
        report = importer.import_option_snapshot(
            db, chain, underlying=symbol, expiry=expiry, spot=spot,
            source=settings.broker, timeframe=timeframe,
            lot_size=settings.lot_size)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return report.to_dict()


@router.get("/coverage")
def coverage(symbol: str = "NIFTY", timeframe: str = "5m",
             db: Session = Depends(get_db)):
    """How much history you actually own.

    Check this before trusting a backtest. Free sources look back about 60
    days; this number grows every day the agent runs.
    """
    return repository.coverage(db, symbol, timeframe).to_dict()


@router.get("/quality")
def data_quality(symbol: str = "NIFTY", timeframe: str = "5m",
                 include_options: bool = True,
                 db: Session = Depends(get_db)):
    """Everything wrong with the stored dataset, worst first.

    A `verdict` of "unusable" means at least one finding is a data error
    rather than a caveat — fix it before reading any backtest run on this
    data.
    """
    return quality.report(db, symbol, timeframe, include_options=include_options)


@router.get("/readiness")
def research_readiness(symbol: str = "NIFTY", timeframe: str = "5m",
                       db: Session = Depends(get_db)):
    """Per-session research readiness: the exchange grid, restatements, and
    option quote, bid/ask, OI, capture-clock and contract-metadata coverage.
    Diagnostic only; nothing here reads or judges a strategy."""
    return readiness.report(db, symbol, timeframe, underlying=symbol)


@router.get("/methodology")
def methodology_readiness(symbol: str = "NIFTY", timeframe: str = "5m",
                          db: Session = Depends(get_db)):
    """Research methodology readiness (Pass 2D): how the archive is classified,
    whether a prospective holdout exists, the trial registry, benchmarks and
    the MTM ledger — one status per question. Reads no strategy output."""
    from ..data import research
    from ..methodology import readiness as methodology
    from ..methodology import registry

    # A trusted data-quality read: the summary counts sessions and their
    # categories, and returns no bar of any of them.
    archive = research.load_research_candles(
        db, symbol, timeframe, access=registry.trusted_access(registry.DATA_QUALITY))
    option = readiness.report(db, symbol, timeframe, underlying=symbol)
    return methodology.summary(db, archive,
                               option_execution=option["verdicts"]["option_execution"])


@router.get("/datasets")
def datasets(limit: int = Query(25, ge=1, le=200), db: Session = Depends(get_db)):
    """Recently fingerprinted datasets.

    Every backtest registers the exact rows it read. This is how you answer
    "did that result change because the strategy changed, or because the
    data did?" — a question that is unanswerable without it.
    """
    return {"datasets": dataset_module.recent(db, limit)}


@router.post("/regimes/backfill", dependencies=[Depends(require_api_key)])
def backfill_regimes(symbol: str = "NIFTY", timeframe: str = "5m",
                     rebuild: bool = False, db: Session = Depends(get_db)):
    """Classify every stored candle into a market regime.

    Idempotent — it upserts, so re-running it after new candles arrive costs
    only the new bars' worth of change. Key-guarded because it writes.

    `rebuild=true` clears the table first. That is the tool for the case
    `engine_version` exists to catch: once the classifier's thresholds
    change, a table holding both generations produces regime splits that
    look like findings and are artefacts of the mix. Upserting alone would
    leave any bar whose candle has since been dropped sitting there under
    the old version.
    """
    removed = regime_store.clear(db, symbol, timeframe) if rebuild else 0
    report = regime_store.backfill(db, symbol, timeframe)
    return report.to_dict() | {"cleared": removed}


@router.get("/regimes")
def regime_coverage(symbol: str = "NIFTY", timeframe: str = "5m",
                    db: Session = Depends(get_db)):
    """How much of the archive has been classified, and by which version.

    `mixed_versions` is the one to read: true means the table holds more than
    one definition of the same label and no split over it is trustworthy.
    """
    return regime_store.coverage(db, symbol, timeframe)
