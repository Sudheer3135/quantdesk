from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..backtest.engine import run
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
