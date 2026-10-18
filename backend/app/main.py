"""FastAPI application entry point."""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import runtime_provenance
from .api import backtest, data, health, journal, market, news, signals, stream
from .api import strategy_v2 as strategy_v2_api
from .brokers.base import UnknownSymbol
from .config import get_settings
from .db import init_db
from .migration_guard import Step, Writer, release_after_drain, scheduler_drained, writer_lease
from .redact import install_log_redaction, settings_secrets
from .security import verify_startup
from .shutdown_policy import check_supervisor
from .strategy_v2 import paper as v2_paper
from .workers import agent, angel_feed, chain_publisher, option_collector, ticker, watchdog

settings = get_settings()
logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Before the first request can be logged. uvicorn's access and error loggers
# do not propagate to the root logger, so a filter there never saw the
# dashboard's `?key=` — this redacts every handler's output (Pass 2E-B).
install_log_redaction(settings.database_url, settings_secrets(settings))

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background jobs, then stop them.

    Replaces the `@app.on_event` hooks, which are deprecated and removed in
    current FastAPI — and therefore blocked the upgrade that carries the
    Starlette security fixes. The scheduler is a local rather than a module
    global now, so its lifetime is visibly tied to the application's.
    """
    # What the repository looked like as this application initialized, held
    # unchanged for the run and stamped on every observation it produces.
    # First, so it predates every scheduled writer (runtime_provenance).
    runtime_provenance.initialize()

    # Before anything else. A production deployment with no key is a
    # misconfiguration, and one that fails loudly here gets fixed rather
    # than shipped.
    verify_startup()
    # A shutdown that cannot finish before the supervisor's SIGKILL — or
    # settings that make no sense — refuse the start (Pass 2E-B).
    check_supervisor(settings)

    # The API process hosts every scheduled writer, so it is one writer to
    # the migration protocol: the shared schema lock is taken, the schema
    # verified under it, and the lock held until everything below has
    # stopped. A migration in progress or a schema that is not this code's
    # head refuses startup here, before any job can write (Pass 2E-A.1).
    lease = writer_lease("api", heartbeat=settings.writer_lease_heartbeat_seconds).acquire()
    init_db()

    # Share one scheduler. Three jobs, three different reasons:
    #   agent            — every few minutes, the strategy's timeframe
    #   ticker           — every few seconds, so the tape looks alive
    #   option_collector — faster than the bar width, so option bars have a
    #                      range instead of a single sampled price
    scheduler = agent.start()
    ticker.start(scheduler)
    option_collector.start(scheduler)

    # Attached to the one scheduler all three share. Every job runs with
    # `max_instances=1`, so a job that overruns its interval has its next
    # run silently skipped — which is how the desk lost two sessions of
    # option data while looking perfectly healthy. This gives that skip a
    # voice; see `/health/scheduler`.
    watchdog.attach(scheduler)

    # The Angel One websocket, if it is configured. Started after the
    # scheduler so the ticker exists to fall back to, and never allowed to
    # stop the application from booting: a missing credential leaves the
    # desk on the polled feed and says so in the log, rather than taking the
    # whole API down with it.
    angel_feed.start()

    # Fans the streamed chain out on the same socket as the price. Started
    # after the feed so there is something to publish, and a no-op unless
    # option streaming is switched on.
    chain_publisher.start()

    # Strategy v2 on paper. Last to start and first to stop: it reads the
    # signal, the price, VIX and the chain, and owns none of them.
    v2_paper.start()

    try:
        yield
    finally:
        # Every in-process database writer is stopped and positively
        # confirmed drained before the lease is released (Pass 2E-A.2/3).
        # Same order as ever: the paper trader first; the chain publisher
        # before the feed that supplies it; the feed before the scheduler, so
        # the ticker serves prices while the socket closes. The publisher and
        # the feed write to Redis only, so they are Steps and never gate the
        # lease. A writer whose stop raises, or that is still running when
        # its stop returns, keeps the lease held and ends the process.
        release_after_drain(lease, [
            # Its stop is a timed join; only the thread being gone counts.
            Writer("v2-paper", v2_paper.stop, lambda: not v2_paper.TRADER.running),
            Step("chain-publisher", chain_publisher.stop),
            Step("angel-feed", angel_feed.stop),
            Writer("scheduler", lambda: scheduler.shutdown(wait=True),
                   lambda: scheduler_drained(scheduler)),
        ], timeout=settings.shutdown_drain_seconds)   # app.shutdown_policy


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="AI-assisted market analysis for NIFTY. Analysis tool, not advice.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (health.router, market.router, signals.router,
               journal.router, backtest.router, stream.router, data.router,
               news.router, strategy_v2_api.router):
    app.include_router(router)


@app.exception_handler(UnknownSymbol)
async def unknown_symbol(request: Request, exc: UnknownSymbol) -> JSONResponse:
    """An unsupported symbol is the caller's mistake, not a server fault.

    Registered centrally so every endpoint taking a `symbol` answers the
    same way, and so adding one later cannot forget to.
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/")
def root():
    return {"service": settings.app_name, "docs": "/docs", "health": "/health"}
