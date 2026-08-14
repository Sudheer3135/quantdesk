"""Backtest endpoints — database-first.

These used to call `get_broker().candles(...)`, which on the free path means
Yahoo, which caps intraday history at 59 days. Every statistic the platform
produced was therefore computed on about two months of data, refetched on
each run, and reproducible on none of them: the same request on two
different days silently ran on two different datasets.

Now the database is the source. It reaches as far back as you have
accumulated, it does not change underneath you, and every result carries a
hash naming the exact rows it read.

When the archive does not cover the requested window this returns **409
rather than a shorter backtest**. That is the whole point. A run that
quietly used six weeks when you asked for two years produces statistics that
look completely normal — there is nothing in a Sharpe ratio that says which
window it came from — and you would act on them.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..backtest import diagnostics
from ..backtest.engine import run
from ..backtest.option_engine import run as run_options
from ..config import get_settings
from ..data import dataset as dataset_module
from ..data import repository
from ..db import get_db
from ..deps import get_broker
from ..risk.manager import RiskConfig

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestIn(BaseModel):
    symbol: str = "NIFTY"
    timeframe: str = "5m"

    # The window. `start`/`end` are the database-first way to ask; `days` is
    # kept for the broker escape hatch, where a date range has no meaning
    # because the source decides how far back it is willing to go.
    start: date | None = None
    end: date | None = None
    days: int = Field(default=30, ge=5, le=3650)

    # "db" reads stored history. "broker" pulls live and is for smoke tests
    # only — it is labelled in the result so such a run can never be
    # mistaken for a reproducible one.
    source: str = "db"

    starting_capital: float = 100_000
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    cost_per_round_trip: float | None = None
    slippage_pct: float = 0.02
    min_sessions: int = Field(default=5, ge=1)


def _risk(payload: BacktestIn) -> RiskConfig:
    return RiskConfig(
        capital=payload.starting_capital,
        risk_per_trade_pct=payload.risk_per_trade_pct,
        max_trades_per_day=payload.max_trades_per_day,
        min_risk_reward=payload.min_risk_reward,
        lot_size=get_settings().lot_size,
    )


def load_candles(db: Session, payload: BacktestIn) -> tuple:
    """Candles plus the block describing exactly what was loaded.

    Raises 409 with a coverage report when the database cannot serve the
    window. The response says what is held and what to run to fix it, which
    is more useful than a shorter backtest and considerably more honest.
    """
    if payload.source == "broker":
        try:
            candles = get_broker().candles(
                payload.symbol, payload.timeframe, payload.days)
        except Exception as exc:
            raise HTTPException(502, f"could not load candles: {exc}") from exc

        print_ = dataset_module.fingerprint(candles, payload.symbol, payload.timeframe)
        block = print_.to_dict()
        block["mode"] = "broker"
        block["caveats"] = block["caveats"] + [
            "Pulled live from the broker, not from stored history. This run "
            "is not reproducible: the same request tomorrow reads a "
            "different window."
        ]
        return candles, block

    gap = repository.check_coverage(
        db, payload.symbol, payload.timeframe,
        start=payload.start, end=payload.end, min_sessions=payload.min_sessions)
    if gap is not None:
        raise HTTPException(409, gap.to_dict())

    candles = repository.load_index_candles(
        db, payload.symbol, payload.timeframe, start=payload.start, end=payload.end)

    print_ = dataset_module.fingerprint(candles, payload.symbol, payload.timeframe)
    dataset_module.register(db, print_)
    block = print_.to_dict()
    block["mode"] = "db"
    return candles, block


@router.post("/run")
def run_backtest(payload: BacktestIn, db: Session = Depends(get_db)):
    """Backtest the strategy on the index itself."""
    candles, block = load_candles(db, payload)
    result = run(
        candles,
        starting_capital=payload.starting_capital,
        risk_config=_risk(payload),
        cost_per_round_trip=(payload.cost_per_round_trip
                             if payload.cost_per_round_trip is not None else 120.0),
        slippage_pct=payload.slippage_pct,
        dataset=block,
    )
    return result.to_dict()


class OptionBacktestIn(BacktestIn):
    """Same rules, but priced as the option you would actually buy."""
    iv: float = 0.13
    strike_offset: int = 0        # 0 = at the money, negative = in the money
    expiry_weekday: int = 1       # 1 = Tuesday; verify against the NSE circular
    slippage_points: float | None = None


@router.post("/options")
def run_option_backtest(payload: OptionBacktestIn, db: Session = Depends(get_db)):
    """Backtest the strategy as an option buyer.

    Expect worse numbers than /backtest/run. The index backtest asks whether
    the direction was right; this asks whether buying the option would have
    made money, which is the question that decides your account.

    Premiums are still modelled with Black-Scholes at a constant IV, because
    there is no free source of historical option data — NSE publishes a
    snapshot, not a tape. The `assumptions` block says so on every run
    rather than leaving it to be discovered.
    """
    candles, block = load_candles(db, payload)
    result = run_options(
        candles,
        starting_capital=payload.starting_capital,
        risk_config=_risk(payload),
        iv=payload.iv,
        strike_offset=payload.strike_offset,
        expiry_weekday=payload.expiry_weekday,
        lot_size=get_settings().lot_size,
        cost_per_round_trip=payload.cost_per_round_trip,
        slippage_points=payload.slippage_points,
        dataset=block,
    )
    return result.to_dict()


@router.post("/diagnose")
def diagnose(payload: OptionBacktestIn, db: Session = Depends(get_db)):
    """Run the option backtest, then break the trades apart to show where
    the money went.

    Use this before changing any setting. A losing backtest tells you the
    strategy does not work; this tells you which part of it does not.
    """
    result = run_option_backtest(payload, db)
    report = diagnostics.report(result)
    report["dataset"] = result.get("dataset", {})
    report["assumptions"] = result.get("assumptions", {})
    return report


class StopSweepIn(OptionBacktestIn):
    """One hypothesis, tested across stop widths."""
    multiples: list[float] = [0.8, 1.2, 1.6, 2.0, 2.5]


@router.post("/stop-sweep")
def stop_sweep(payload: StopSweepIn, db: Session = Depends(get_db)):
    """Test one specific idea: is the stop too tight?

    Runs the same strategy at several ATR multiples for the stop. This is a
    single hypothesis, not a search — if you widen it to sweep every
    parameter at once you will find a combination that fits the noise in
    your sample and fails on new data.

    Read the shape, not the winner. A clean trend across multiples is
    evidence. A single spike in the middle is luck.
    """
    from ..analytics import signal_engine

    candles, block = load_candles(db, payload)

    rows = []
    for multiple in payload.multiples:
        def signal_fn(frame, m=multiple):
            return signal_engine.generate(frame, atr_stop_multiple=m)

        result = run_options(
            candles,
            starting_capital=payload.starting_capital,
            risk_config=_risk(payload),
            iv=payload.iv,
            strike_offset=payload.strike_offset,
            expiry_weekday=payload.expiry_weekday,
            lot_size=get_settings().lot_size,
            cost_per_round_trip=payload.cost_per_round_trip,
            slippage_points=payload.slippage_points,
            signal_fn=signal_fn,
            dataset=block,
        )
        stats = result.stats
        rows.append({
            "atr_stop_multiple": multiple,
            "trades": stats.get("trades", 0),
            "win_rate_pct": stats.get("win_rate_pct"),
            "expectancy_r": stats.get("expectancy_r"),
            "net_pnl": stats.get("net_pnl"),
            "max_drawdown_pct": stats.get("max_drawdown_pct"),
        })

    return {
        "results": rows,
        # Every row above ran on exactly these bars. Without the hash, a
        # sweep repeated next week against a grown archive looks like a
        # comparison and is not one.
        "dataset": block,
        "warning": "One sample of roughly a hundred trades. Treat a clean "
                   "trend as evidence and a single spike as luck. Do not "
                   "adopt the best row as a setting.",
    }
