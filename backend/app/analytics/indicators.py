"""Price indicators.

Every function takes a DataFrame with columns:
    timestamp (tz-aware), open, high, low, close, volume
and returns a Series aligned to the same index.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import market_hours

REQUIRED_COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def validate(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"candle frame is missing columns: {missing}")
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out.sort_values("timestamp").reset_index(drop=True)


def ema(df: pd.DataFrame, length: int, source: str = "close") -> pd.Series:
    return df[source].ewm(span=length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Wilder's ATR — same maths TradingView uses for ATR(14)."""
    return true_range(df).ewm(alpha=1 / length, adjust=False).mean()


def session_key(df: pd.DataFrame, tz: str = "Asia/Kolkata") -> pd.Series:
    """Trading date in IST. VWAP resets on this boundary."""
    return df["timestamp"].dt.tz_convert(tz).dt.date


def volume_weights(df: pd.DataFrame) -> pd.Series:
    """Never infer traded volume from the index feed's 0/1/2 placeholders."""
    volume = pd.to_numeric(df["volume"], errors="coerce")
    valid = volume.notna() & (volume > 2)
    if "volume_is_synthetic" in df:
        valid &= ~df["volume_is_synthetic"].fillna(True).astype(bool)
    # Repository provenance is conservative for mixed-source frames.
    if any(isinstance(v, dict) and v.get("volume_is_synthetic")
           for v in df.attrs.values()):
        valid[:] = False
    if df.attrs.get("volume_is_synthetic"):
        valid[:] = False
    return volume.where(valid)


def has_real_volume(df: pd.DataFrame) -> bool:
    return not df.empty and bool(volume_weights(df).notna().all())


def vwap_bands(df: pd.DataFrame, stdevs: float = 1.0, tz: str = "Asia/Kolkata"):
    """Weighted population variance E[p²] - E[p]², reset per IST session.

    A missing/placeholder weight invalidates that session's cumulative VWAP
    from that point forward. We do not interpolate missing traded volume.
    """
    price = (df["high"] + df["low"] + df["close"]) / 3
    key = session_key(df, tz)
    weights = volume_weights(df)
    complete = weights.notna().groupby(key).cummin()
    total = weights.fillna(0).groupby(key).cumsum().replace(0, np.nan)
    mean = ((price * weights).fillna(0).groupby(key).cumsum() / total).where(complete)
    second = ((price**2 * weights).fillna(0).groupby(key).cumsum() / total).where(complete)
    deviation = np.sqrt((second - mean**2).clip(lower=0))
    return mean, mean + stdevs * deviation, mean - stdevs * deviation


def vwap(df: pd.DataFrame, tz: str = "Asia/Kolkata") -> pd.Series:
    return vwap_bands(df, tz=tz)[0]


def relative_volume(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """Current bar volume divided by its recent average. >1.5 is a real push."""
    weights = volume_weights(df)
    avg = weights.rolling(length, min_periods=max(2, length // 2)).mean()
    complete = weights.notna().rolling(length, min_periods=1).min().astype(bool)
    return (weights / avg.replace(0, np.nan)).where(complete)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the standard indicator set used across the whole platform."""
    out = validate(df)
    out["ema20"] = ema(out, 20)
    out["ema50"] = ema(out, 50)
    out["ema100"] = ema(out, 100)
    out["ema200"] = ema(out, 200)
    out["atr14"] = atr(out, 14)
    vw, up, lo = vwap_bands(out)
    out["vwap"] = vw
    out["vwap_upper"] = up
    out["vwap_lower"] = lo
    out["rvol"] = relative_volume(out)
    return out


TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "60m": 60, "1d": 1440,
}


def drop_outside_session(df: pd.DataFrame, tz: str = "Asia/Kolkata") -> pd.DataFrame:
    """Remove candles from weekends and from outside 09:15-15:30 IST.

    Boundary alignment alone is not enough. The mock broker steps forward
    five minutes at a time with no notion of weekends, so its candles land
    on perfectly valid 5-minute boundaries on a Sunday afternoon. Once mixed
    into the archive they are invisible — and a backtest that trades a
    Sunday is not measuring anything real.

    This does not catch exchange holidays, which need a calendar. Weekends
    and clock hours remove the overwhelming majority of impossible bars.
    """
    if df.empty:
        return df

    local = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(tz)
    weekday = local.dt.dayofweek < 5                      # Monday-Friday
    # market_hours.MARKET_OPEN/CLOSE are already plain `time` objects — this
    # used to restate the exchange's open and close as its own (hour,
    # minute) tuples and rebuild a time from a throwaway Timestamp, one more
    # copy of the session boundary the market-hours guard test could not
    # see because it only looked for a `datetime.time(...)` constructor
    # call, not a bare tuple written out the same way.
    open_t = market_hours.MARKET_OPEN
    close_t = market_hours.MARKET_CLOSE
    in_hours = (local.dt.time >= open_t) & (local.dt.time <= close_t)

    keep = weekday & in_hours
    dropped = int((~keep).sum())
    if dropped:
        logging.getLogger(__name__).info(
            "dropped %s candles outside NSE session hours", dropped)
    return df[keep].reset_index(drop=True)


def drop_future(df: pd.DataFrame) -> pd.DataFrame:
    """Remove candles dated after now.

    Nothing legitimate produces these. A misconfigured generator dated bars
    two months ahead and they sat in the archive looking exactly like real
    data — the count went up, nothing errored, and the only visible symptom
    was a "last candle" date that had not happened yet.

    A cheap absolute guard is worth more here than a clever one.
    """
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC")
    keep = pd.to_datetime(df["timestamp"], utc=True) <= now
    dropped = int((~keep).sum())
    if dropped:
        logging.getLogger(__name__).warning(
            "dropped %s candles dated in the future — check the data source", dropped)
    return df[keep].reset_index(drop=True)


def drop_unclosed(df: pd.DataFrame, timeframe: str, as_of=None) -> pd.DataFrame:
    """Remove candles that have not finished forming.

    A live feed hands you the bar currently in progress. Its high, low and
    close are still moving, so storing it is storing a lie: the archive
    claims a final value for something that has not settled.

    This matters more than it sounds. A partial bar has an artificially
    narrow range, which understates ATR, drags VWAP, and — in a backtest —
    produces a candle that never actually existed at that shape. Worse, it
    is silent: nothing downstream can tell a partial bar from a real one.

    A closed 5-minute candle always starts on a 5-minute boundary with zero
    seconds. Anything else is mid-formation and gets dropped.
    """
    if df.empty:
        return df

    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if not minutes:
        logging.getLogger(__name__).warning(
            "unknown timeframe %r — cannot check for unclosed bars", timeframe)
        return df

    ts = pd.to_datetime(df["timestamp"], utc=True)
    aligned = (ts.dt.second == 0) & (ts.dt.microsecond == 0)
    if minutes < 1440:
        aligned &= (ts.dt.minute % minutes == 0)

    if (~aligned).any():
        for bad in ts[~aligned]:
            logging.getLogger(__name__).info("dropping unclosed %s candle at %s", timeframe, bad)

    now = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now(tz="UTC")
    if now.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    complete = ts + pd.Timedelta(minutes=minutes) <= now
    return df[aligned & complete].reset_index(drop=True)
