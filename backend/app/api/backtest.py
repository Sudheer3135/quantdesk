from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..backtest import diagnostics
from ..backtest.engine import run
from ..backtest.option_engine import run as run_options
from ..config import get_settings
from ..deps import get_broker
from ..risk.manager import RiskConfig

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestIn(BaseModel):
    symbol: str = "NIFTY"
    timeframe: str = "5m"
    days: int = Field(default=30, ge=5, le=365)
    starting_capital: float = 100_000
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    cost_per_round_trip: float = 120.0
    slippage_pct: float = 0.02


@router.post("/run")
def run_backtest(payload: BacktestIn):
    s = get_settings()
    try:
        candles = get_broker().candles(payload.symbol, payload.timeframe, payload.days)
    except Exception as exc:
        raise HTTPException(502, f"could not load candles: {exc}") from exc

    result = run(
        candles,
        starting_capital=payload.starting_capital,
        risk_config=RiskConfig(
            capital=payload.starting_capital,
            risk_per_trade_pct=payload.risk_per_trade_pct,
            max_trades_per_day=payload.max_trades_per_day,
            min_risk_reward=payload.min_risk_reward,
            lot_size=s.lot_size,
        ),
        cost_per_round_trip=payload.cost_per_round_trip,
        slippage_pct=payload.slippage_pct,
    )
    return result.to_dict()


class OptionBacktestIn(BacktestIn):
    """Same rules, but priced as the option you would actually buy."""
    iv: float = 0.13
    strike_offset: int = 0        # 0 = at the money, negative = in the money
    expiry_weekday: int = 1       # 1 = Tuesday; verify against the NSE circular
    slippage_points: float = 0.5


@router.post("/options")
def run_option_backtest(payload: OptionBacktestIn):
    """Backtest the strategy as an option buyer.

    Expect worse numbers than /backtest/run. The index backtest asks whether
    the direction was right; this asks whether buying the option would have
    made money, which is the question that decides your account.
    """
    s = get_settings()
    try:
        candles = get_broker().candles(payload.symbol, payload.timeframe, payload.days)
    except Exception as exc:
        raise HTTPException(502, f"could not load candles: {exc}") from exc

    result = run_options(
        candles,
        starting_capital=payload.starting_capital,
        risk_config=RiskConfig(
            capital=payload.starting_capital,
            risk_per_trade_pct=payload.risk_per_trade_pct,
            max_trades_per_day=payload.max_trades_per_day,
            min_risk_reward=payload.min_risk_reward,
            lot_size=s.lot_size,
        ),
        iv=payload.iv,
        strike_offset=payload.strike_offset,
        expiry_weekday=payload.expiry_weekday,
        lot_size=s.lot_size,
        cost_per_round_trip=payload.cost_per_round_trip,
        slippage_points=payload.slippage_points,
    )
    return result.to_dict()


@router.post("/diagnose")
def diagnose(payload: OptionBacktestIn):
    """Run the option backtest, then break the trades apart to show where
    the money went.

    Use this before changing any setting. A losing backtest tells you the
    strategy does not work; this tells you which part of it does not.
    """
    result = run_option_backtest(payload)
    return diagnostics.report(result)


class StopSweepIn(OptionBacktestIn):
    """One hypothesis, tested across stop widths."""
    multiples: list[float] = [0.8, 1.2, 1.6, 2.0, 2.5]


@router.post("/stop-sweep")
def stop_sweep(payload: StopSweepIn):
    """Test one specific idea: is the stop too tight?

    Runs the same strategy at several ATR multiples for the stop. This is a
    single hypothesis, not a search — if you widen it to sweep every
    parameter at once you will find a combination that fits the noise in
    your sample and fails on new data.

    Read the shape, not the winner. A clean trend across multiples is
    evidence. A single spike in the middle is luck.
    """
    from ..analytics import signal_engine

    s = get_settings()
    try:
        candles = get_broker().candles(payload.symbol, payload.timeframe, payload.days)
    except Exception as exc:
        raise HTTPException(502, f"could not load candles: {exc}") from exc

    rows = []
    for multiple in payload.multiples:
        def signal_fn(frame, m=multiple):
            return signal_engine.generate(frame, atr_stop_multiple=m)

        result = run_options(
            candles,
            starting_capital=payload.starting_capital,
            risk_config=RiskConfig(
                capital=payload.starting_capital,
                risk_per_trade_pct=payload.risk_per_trade_pct,
                max_trades_per_day=payload.max_trades_per_day,
                min_risk_reward=payload.min_risk_reward,
                lot_size=s.lot_size,
            ),
            iv=payload.iv,
            strike_offset=payload.strike_offset,
            expiry_weekday=payload.expiry_weekday,
            lot_size=s.lot_size,
            cost_per_round_trip=payload.cost_per_round_trip,
            slippage_points=payload.slippage_points,
            signal_fn=signal_fn,
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
        "warning": "One sample of roughly a hundred trades. Treat a clean "
                   "trend as evidence and a single spike as luck. Do not "
                   "adopt the best row as a setting.",
    }