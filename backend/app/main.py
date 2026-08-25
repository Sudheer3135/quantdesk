"""FastAPI application entry point."""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import backtest, data, health, journal, market, signals, stream
from .brokers.base import UnknownSymbol
from .config import get_settings
from .db import init_db
from .security import verify_startup
from .workers import agent, option_collector, ticker, watchdog

settings = get_settings()
logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background jobs, then stop them.

    Replaces the `@app.on_event` hooks, which are deprecated and removed in
    current FastAPI — and therefore blocked the upgrade that carries the
    Starlette security fixes. The scheduler is a local rather than a module
    global now, so its lifetime is visibly tied to the application's.
    """
    # Before anything else. A production deployment with no key is a
    # misconfiguration, and one that fails loudly here gets fixed rather
    # than shipped.
    verify_startup()
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

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


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
               journal.router, backtest.router, stream.router, data.router):
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
