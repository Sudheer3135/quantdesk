"""FastAPI application entry point."""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import backtest, data, health, journal, market, signals, stream
from .config import get_settings
from .db import init_db
from .workers import agent, option_collector, ticker

settings = get_settings()
logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="AI-assisted market analysis for NIFTY. Analysis tool, not advice.",
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

_scheduler = None


@app.on_event("startup")
def on_startup():
    global _scheduler
    init_db()
    _scheduler = agent.start()
    # Share one scheduler. Three jobs, three different reasons:
    #   agent            — every few minutes, the strategy's timeframe
    #   ticker           — every few seconds, so the tape looks alive
    #   option_collector — faster than the bar width, so option bars have a
    #                      range instead of a single sampled price
    ticker.start(_scheduler)
    option_collector.start(_scheduler)


@app.on_event("shutdown")
def on_shutdown():
    if _scheduler:
        _scheduler.shutdown(wait=False)


@app.get("/")
def root():
    return {"service": settings.app_name, "docs": "/docs", "health": "/health"}
