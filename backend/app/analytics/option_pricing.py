"""Option pricing and greeks.

NIFTY index options are European and cash-settled, so Black-Scholes is the
right model rather than an approximation of one.

Why this module has to exist: every signal so far has been about the index,
but the instrument you would actually buy is an option. Those behave
differently in ways that decide whether a strategy makes money.

  - A call does not move point-for-point with the index. It moves by delta,
    roughly 0.5 at the money. A 30-point index move is a 15-point premium
    move, so sizing against index points overstates your risk by about
    double.
  - Time decay runs against a buyer every minute the position is open. You
    can be right about direction, hit your target late, and still lose.
  - Decay accelerates as expiry approaches. The same trade held on Tuesday
    of expiry week is a materially worse trade than on the previous
    Thursday.

None of that shows up in an index backtest. All of it shows up in your
account.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

OptionType = Literal["CE", "PE"]

# Trading days, not calendar days. Decay is driven by sessions, and using
# 365 systematically understates theta for short-dated positions.
TRADING_DAYS_PER_YEAR = 252
MINUTES_PER_SESSION = 375

# NSE index options carry no dividend adjustment and the risk-free rate
# barely moves the price at these tenors, but both are exposed so you can
# change them without editing the maths.
DEFAULT_RATE = 0.065
DEFAULT_IV = 0.13


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float          # premium lost per trading day
    vega: float           # premium change per 1 point of IV

    def to_dict(self) -> dict:
        return asdict(self)


def _d1_d2(spot: float, strike: float, years: float, iv: float, rate: float):
    vol_time = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years) / vol_time
    return d1, d1 - vol_time


def price(spot: float, strike: float, years: float, iv: float = DEFAULT_IV,
          rate: float = DEFAULT_RATE, kind: OptionType = "CE") -> float:
    """Black-Scholes premium.

    At or past expiry the option is worth its intrinsic value only, which is
    also the correct answer for a zero-volatility input — both cases fall
    through to the same branch rather than dividing by zero.
    """
    if years <= 0 or iv <= 0:
        return max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)

    d1, d2 = _d1_d2(spot, strike, years, iv, rate)
    discount = math.exp(-rate * years)

    if kind == "CE":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def greeks(spot: float, strike: float, years: float, iv: float = DEFAULT_IV,
           rate: float = DEFAULT_RATE, kind: OptionType = "CE") -> Greeks:
    """Premium plus the sensitivities that decide a buyer's outcome."""
    if years <= 0 or iv <= 0:
        intrinsic = max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
        in_the_money = intrinsic > 0
        return Greeks(
            price=intrinsic,
            delta=(1.0 if kind == "CE" else -1.0) if in_the_money else 0.0,
            gamma=0.0, theta=0.0, vega=0.0,
        )

    d1, d2 = _d1_d2(spot, strike, years, iv, rate)
    discount = math.exp(-rate * years)
    pdf = _norm_pdf(d1)

    delta = _norm_cdf(d1) if kind == "CE" else _norm_cdf(d1) - 1.0
    gamma = pdf / (spot * iv * math.sqrt(years))

    # Annual theta, converted to per-trading-day so the number reads the way
    # a trader thinks about it: "this costs me X points a day to hold".
    common = -(spot * pdf * iv) / (2 * math.sqrt(years))
    if kind == "CE":
        annual_theta = common - rate * strike * discount * _norm_cdf(d2)
    else:
        annual_theta = common + rate * strike * discount * _norm_cdf(-d2)

    return Greeks(
        price=price(spot, strike, years, iv, rate, kind),
        delta=delta,
        gamma=gamma,
        theta=annual_theta / TRADING_DAYS_PER_YEAR,
        vega=spot * pdf * math.sqrt(years) / 100,
    )


def years_to_expiry(now: datetime, expiry: datetime) -> float:
    """Time remaining, measured in trading years.

    Calendar time overstates how much life a short-dated option has: a
    Friday-afternoon option facing a weekend loses far less value over those
    two days than calendar maths implies, because the market is shut. Using
    sessions keeps decay attached to the thing that actually causes it.
    """
    if expiry <= now:
        return 0.0

    delta = expiry - now
    calendar_days = delta.total_seconds() / 86400
    # Roughly 5 trading days in every 7 calendar days.
    trading_days = calendar_days * (5 / 7)
    return max(trading_days / TRADING_DAYS_PER_YEAR, 0.0)


def implied_volatility(market_price: float, spot: float, strike: float,
                       years: float, rate: float = DEFAULT_RATE,
                       kind: OptionType = "CE",
                       tolerance: float = 1e-5, max_iterations: int = 100) -> float | None:
    """Back out IV from a traded premium, by bisection.

    Bisection rather than Newton-Raphson: it is slower but it cannot diverge,
    and deep out-of-the-money options have a vega near zero that makes
    Newton's step explode exactly when you least want a wrong answer.

    Returns None when no volatility reproduces the price — usually a stale
    or crossed quote, which is worth knowing about rather than papering over.
    """
    if years <= 0 or market_price <= 0:
        return None

    intrinsic = max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
    if market_price < intrinsic:
        return None

    low, high = 1e-4, 5.0
    for _ in range(max_iterations):
        mid = (low + high) / 2
        modelled = price(spot, strike, years, mid, rate, kind)
        if abs(modelled - market_price) < tolerance:
            return mid
        if modelled > market_price:
            high = mid
        else:
            low = mid

    return None if abs(high - low) > 0.01 else (low + high) / 2


def atm_strike(spot: float, step: int = 50) -> float:
    return round(spot / step) * step


def select_strike(spot: float, direction: Literal["BUY", "SELL"],
                  offset: int = 0, step: int = 50) -> tuple[float, OptionType]:
    """Which contract a directional signal actually buys.

    BUY takes a call, SELL takes a put. `offset` moves the strike in steps
    away from at-the-money: negative goes in the money (higher delta, more
    premium, less decay as a share of cost), positive goes out of the money
    (cheaper, but a larger share of the premium is time value that expires).
    """
    atm = atm_strike(spot, step)
    if direction == "BUY":
        return atm + offset * step, "CE"
    return atm - offset * step, "PE"