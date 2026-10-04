"""Builders shared by the option-buying tests.

Real 5-minute sessions and a real strike ladder, assembled deterministically
so a test can state the exact market it means. Nothing here is a mock of the
platform's own code: the candles go through `HistoricalFeed` and the option
rows through `ChainStore` exactly as production data does.
"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import option_pricing  # noqa: E402
from app.market_hours import IST  # noqa: E402
from app.models import OptionCandle, OptionContract  # noqa: E402
from app.optionbuy.chain import ChainStore, ContractKey, OptionBar  # noqa: E402

# 09:15 IST is 03:45 UTC. Stated as UTC so the fixture never depends on the
# machine's zone.
SESSION_OPEN_UTC = (3, 45)
BARS_PER_SESSION = 75            # 09:15 -> 15:25, the last bar that starts
                                 # inside the session
BUCKET_MINUTES = 5
POLLS_PER_BUCKET = 5             # a 60s poll folded into a 5m bucket


def session_stamps(day: date, bars: int = BARS_PER_SESSION) -> list[datetime]:
    """Bucket starts for one session, in UTC."""
    hour, minute = SESSION_OPEN_UTC
    first = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return [first + timedelta(minutes=BUCKET_MINUTES * n) for n in range(bars)]


def sessions(first: date, count: int) -> list[date]:
    """`count` consecutive weekdays starting at or after `first`."""
    out: list[date] = []
    day = first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def candles(days: list[date], *, base: float = 24_000.0, drift: float = 1.5,
            bars: int = BARS_PER_SESSION, wick: float = 6.0,
            volume: float = 1_000.0) -> pd.DataFrame:
    """A clean 5-minute index series, one row per bucket per session.

    A steady drift rather than a random walk: every test that asserts on a
    stop or a target needs to know where price went, and a seeded random
    series moves that knowledge into the seed.
    """
    rows = []
    price = base
    for day in days:
        for stamp in session_stamps(day, bars):
            open_ = price
            close = price + drift
            rows.append({
                "timestamp": stamp,
                "open": open_,
                "high": max(open_, close) + wick,
                "low": min(open_, close) - wick,
                "close": close,
                "volume": volume,
            })
            price = close
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def ladder(spot: float, step: int = 50, width: int = 4) -> list[float]:
    atm = option_pricing.atm_strike(spot, step)
    return [atm + n * step for n in range(-width, width + 1)]


def option_bars(
    days: list[date],
    expiry: date,
    *,
    strikes: list[float] | None = None,
    spot_at: dict[datetime, float] | None = None,
    bar_kind: str = "snapshot",
    samples: int = POLLS_PER_BUCKET,
    bars: int = BARS_PER_SESSION,
    open_interest: float = 900_000.0,
    volume: float = 250_000.0,
    iv: float = 0.13,
    bid_ask_spread: float | None = None,
    types: tuple[str, ...] = ("CE", "PE"),
) -> dict[ContractKey, list[OptionBar]]:
    """A stored chain, priced off the index with Black-Scholes.

    Priced from the model on purpose: the point of a fixture is that the
    premium is a known function of the spot, so a test can assert that the
    engine used the *stored* number rather than recomputing one. The stored
    rows are then indistinguishable from collected ones as far as the engine
    is concerned — which is exactly the property under test.
    """
    strikes = strikes or ladder(24_000.0)
    spot_at = spot_at or {}
    out: dict[ContractKey, list[OptionBar]] = {}
    row_id = 1

    for day in days:
        for stamp in session_stamps(day, bars):
            spot = spot_at.get(stamp, 24_000.0)
            years = option_pricing.years_to_expiry(
                stamp.astimezone(IST),
                datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=IST))
            for strike in strikes:
                for kind in types:
                    premium = max(0.05, option_pricing.price(
                        spot, strike, max(years, 1e-6), iv, kind=kind))
                    key = ContractKey(expiry=expiry, strike=float(strike),
                                      option_type=kind)
                    bid = ask = None
                    if bid_ask_spread is not None:
                        bid = max(0.05, premium - bid_ask_spread / 2)
                        ask = premium + bid_ask_spread / 2
                    out.setdefault(key, []).append(OptionBar(
                        row_id=row_id, contract_id=hash(key) % 100_000, key=key,
                        timestamp=stamp,
                        open=premium, high=premium * 1.01,
                        low=premium * 0.99, close=premium,
                        volume=volume, open_interest=open_interest, iv=iv,
                        bid=bid, ask=ask, underlying_close=spot,
                        bar_kind=bar_kind, source="free", samples=samples,
                        session_date=stamp.astimezone(IST).date(),
                    ))
                    row_id += 1
    return out


def store_for(days: list[date], expiry: date, frame: pd.DataFrame | None = None,
              **kwargs) -> ChainStore:
    """A `ChainStore` whose premiums track the index frame bar for bar."""
    spot_at = {}
    if frame is not None:
        for stamp, close in zip(frame["timestamp"], frame["close"], strict=True):
            spot_at[stamp.to_pydatetime()] = float(close)
        kwargs.setdefault("strikes", ladder(float(frame["close"].iloc[0])))
    bars = option_bars(days, expiry, spot_at=spot_at, **kwargs)
    return ChainStore(bars, {}, underlying="NIFTY", timeframe="5m")


def seed_option_rows(db, days: list[date], expiry: date, *,
                     strikes: list[float] | None = None,
                     bar_kind: str = "snapshot",
                     samples: int = POLLS_PER_BUCKET,
                     bars: int = BARS_PER_SESSION,
                     premium: float = 120.0,
                     open_interest: float = 900_000.0,
                     volume: float = 250_000.0,
                     types: tuple[str, ...] = ("CE", "PE")) -> None:
    """Write real `option_contracts` and `option_candles` rows.

    The DB path and the in-memory path are both exercised because they fail
    differently: a query that joins wrongly produces an empty store, which
    every in-memory test would pass straight through.
    """
    from sqlalchemy import select

    strikes = strikes or ladder(24_000.0)
    contracts: dict[ContractKey, OptionContract] = {}
    for strike in strikes:
        for kind in types:
            key = ContractKey(expiry, float(strike), kind)
            # Idempotent, like the real importer: a second call extends the
            # history of a contract rather than trying to create it twice.
            existing = db.scalars(
                select(OptionContract).where(
                    OptionContract.underlying == "NIFTY",
                    OptionContract.expiry_date == expiry,
                    OptionContract.strike == float(strike),
                    OptionContract.option_type == kind)).first()
            if existing is not None:
                contracts[key] = existing
                continue
            row = OptionContract(
                underlying="NIFTY", expiry_date=expiry, strike=float(strike),
                option_type=kind, lot_size=75,
                tradingsymbol=f"NIFTY{strike:.0f}{kind}",
                first_seen=datetime(days[0].year, days[0].month, days[0].day,
                                    3, 45, tzinfo=UTC),
                last_seen=datetime(days[-1].year, days[-1].month, days[-1].day,
                                   10, 0, tzinfo=UTC),
                source="free")
            db.add(row)
            contracts[key] = row
    db.flush()

    for day in days:
        for stamp in session_stamps(day, bars):
            for contract in contracts.values():
                db.add(OptionCandle(
                    contract_id=contract.id, timeframe="5m", timestamp=stamp,
                    open=premium, high=premium * 1.02, low=premium * 0.98,
                    close=premium, volume=volume, open_interest=open_interest,
                    iv=0.13, underlying_close=24_000.0, bar_kind=bar_kind,
                    source="free", session_date=day, samples=samples,
                    ingested_at=stamp))
    db.commit()


def seed_index_rows(db, frame: pd.DataFrame, symbol: str = "NIFTY",
                    timeframe: str = "5m") -> None:
    from app.models import CandleRecord

    for row in frame.itertuples():
        stamp = row.timestamp.to_pydatetime()
        db.add(CandleRecord(
            symbol=symbol, timeframe=timeframe, timestamp=stamp,
            open=row.open, high=row.high, low=row.low, close=row.close,
            volume=row.volume, source="free",
            session_date=stamp.astimezone(IST).date(),
            ingested_at=stamp, volume_is_synthetic=False))
    db.commit()
