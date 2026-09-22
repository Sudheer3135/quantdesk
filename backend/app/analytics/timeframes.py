"""Folding 5-minute candles into 15-minute and hourly ones.

The bias layer needs a higher-timeframe view, and the archive only holds
5-minute bars. Aggregating them is the obvious move and it hides the single
easiest look-ahead in the whole platform: **the bar currently forming**.

At 10:20 the 15-minute bar covering 10:15–10:30 exists as two of its three
five-minute pieces. Its high, low and close are still moving. Reading it as
though it were finished means reading a bar whose close has not happened —
and the resulting "higher-timeframe trend" would be partly made of the very
move the desk is deciding whether to trade. That is not a small leak; it is
the future arriving one bar early, dressed as context.

So nothing here emits a group until it can no longer change. Two ways a
group becomes final and both are checked:

  it holds its full complement of 5-minute bars, or
  it belongs to a session earlier than the frame's last one, which is over.

The second rule exists because NSE's session is 6h15m, which does not divide
into hours. Every trading day ends with a 15:15–15:30 stub of three bars
that is a legitimate closed hourly bar for that day and would otherwise be
discarded forever. Within the *current* session the stub is still forming,
so it is held back — after the close that costs the hourly view its last
fifteen minutes until the next session starts. Deliberately the conservative
direction: dropping a real bar loses information, keeping an unfinished one
invents it.

Anchored on the session open rather than the wall clock, which is how the
bars are actually read here: 09:15–09:30, 09:30–09:45, and hourly from
09:15. `pandas.resample` would anchor on midnight and put the first bar of
every day in a 09:00–10:00 bucket that begins before the market does.
"""
from __future__ import annotations

import pandas as pd

from . import indicators

# Five-minute bars per higher-timeframe bar.
FIFTEEN_MIN = 3
HOURLY = 12

AGGREGATION = {"open": "first", "high": "max", "low": "min",
               "close": "last", "volume": "sum"}


def fold(df: pd.DataFrame, bars_per_group: int, *, as_of=None) -> pd.DataFrame:
    """Closed higher-timeframe candles, oldest first.

    Returns the platform's standard six columns so that `indicators.enrich`,
    `structure.analyse` and everything else works on the result unchanged —
    a higher-timeframe frame is just a candle frame.
    """
    if bars_per_group < 1:
        raise ValueError("bars_per_group must be at least 1")

    out = indicators.validate(df)
    if not out.empty:
        decision = as_of or df.attrs.get("decision_time") or (out.timestamp.max() + pd.Timedelta(minutes=5))
        out = indicators.drop_unclosed(out, "5m", as_of=decision)
    if out.empty:
        return out.iloc[0:0][indicators.REQUIRED_COLS]

    session = indicators.session_key(out)
    local = out.timestamp.dt.tz_convert("Asia/Kolkata")
    elapsed = local.dt.hour * 60 + local.dt.minute - (9 * 60 + 15)
    out = out[(elapsed >= 0) & (elapsed < 375)].copy()
    session = indicators.session_key(out)
    group = (elapsed.loc[out.index] // (5 * bars_per_group))

    out = out.assign(_session=session, _group=group)

    folded = (out.groupby(["_session", "_group"], sort=True)
              .agg(**{"timestamp": ("timestamp", "first"), **{
                  name: (name, how) for name, how in AGGREGATION.items()}},
                   _bars=("close", "size"))
              .reset_index())

    # A group can still receive bars only if it is the final group of the
    # final session in this frame. Everything else is settled: either it is
    # full, or its session is over.
    last_session = folded["_session"].iloc[-1]
    current = folded["_session"] == last_session
    # No logging here: holding back the forming bar is the correct outcome on
    # every single call during a live session, so it is not an event.
    # Even a past session's missing pieces are not a complete HTF bar.
    # The short final session bucket is permitted only when all its pieces exist.
    local_start = folded.timestamp.dt.tz_convert("Asia/Kolkata")
    expected = ((15 * 60 + 30 - local_start.dt.hour * 60 - local_start.dt.minute) // 5).clip(upper=bars_per_group)
    ends = folded.timestamp + pd.to_timedelta(expected * 5, unit="min")
    closed = (folded["_bars"] == expected) & (ends <= pd.Timestamp(decision))

    result = (folded[closed][indicators.REQUIRED_COLS]
              .sort_values("timestamp").reset_index(drop=True))
    result.attrs = dict(df.attrs)
    result.attrs["volume_is_synthetic"] = not indicators.has_real_volume(df)
    return result


def fifteen_minute(df: pd.DataFrame) -> pd.DataFrame:
    return fold(df, FIFTEEN_MIN)


def hourly(df: pd.DataFrame) -> pd.DataFrame:
    return fold(df, HOURLY)
