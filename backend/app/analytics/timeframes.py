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
        decision = (as_of or df.attrs.get("decision_time")
                    or (out.timestamp.max() + pd.Timedelta(minutes=5)))
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

    # Volume validity has to travel with the constituents, bar by bar. A
    # fifteen-minute bar is only as trustworthy as the least trustworthy
    # five-minute bar inside it, and `sum` on its own says the opposite:
    # it skips what it cannot add and returns a confident number.
    out.attrs = dict(df.attrs)
    out = out.assign(
        _volume_usable=indicators.volume_weights(out).notna(),
        _volume_present=pd.to_numeric(out["volume"], errors="coerce").notna())

    folded = (out.groupby(["_session", "_group"], sort=True)
              .agg(**{"timestamp": ("timestamp", "first"), **{
                  name: (name, how) for name, how in AGGREGATION.items()}},
                   _bars=("close", "size"),
                   # `all` over the bin's own rows only, so a later bar can
                   # never change an aggregate already emitted.
                   _volume_usable=("_volume_usable", "all"),
                   _volume_present=("_volume_present", "all"))
              .reset_index())

    # A group can still receive bars only if it is the final group of the
    # final session in this frame. Everything else is settled: either it is
    # full, or its session is over.
    last_session = folded["_session"].iloc[-1]
    # Unused, but kept: evaluating it is the existing behaviour (and
    # iloc[-1] raises on an empty frame), so a lint fix must not drop it.
    current = folded["_session"] == last_session  # noqa: F841
    # No logging here: holding back the forming bar is the correct outcome on
    # every single call during a live session, so it is not an event.
    # Even a past session's missing pieces are not a complete HTF bar.
    # The short final session bucket is permitted only when all its pieces exist.
    local_start = folded.timestamp.dt.tz_convert("Asia/Kolkata")
    expected = ((15 * 60 + 30 - local_start.dt.hour * 60 - local_start.dt.minute) // 5).clip(
        upper=bars_per_group)
    ends = folded.timestamp + pd.to_timedelta(expected * 5, unit="min")
    closed = (folded["_bars"] == expected) & (ends <= pd.Timestamp(decision))

    kept = folded[closed].sort_values("timestamp").reset_index(drop=True)
    result = kept[indicators.REQUIRED_COLS].copy()

    # A bin is only as good as its constituents, and the *number* has to
    # carry that — not a flag beside it. Keeping the arithmetic sum and
    # marking it untrusted was unsafe: selecting the six standard columns
    # dropped the mark and left a confident 600 on a frame still claiming
    # genuine volume, which then produced a finite VWAP. A total that
    # cannot be trusted is not reported at all.
    #
    # Missing and untrusted are both covered here: `_volume_usable` is
    # false for a NaN bar, a row the source flagged, and every bar of a
    # frame whose provenance is not genuine. A genuine zero stays usable,
    # so [100, 0, 300] still totals 400.
    result["volume"] = result["volume"].where(kept["_volume_usable"])

    result.attrs = dict(df.attrs)
    # Aggregation moves the numbers but not their origin: summing a
    # substitute gives a bigger substitute, and summing traded volume
    # gives traded volume. The claim carries through unchanged rather
    # than being re-derived from what the sums happen to look like.
    provenance = indicators.volume_provenance(df)
    indicators.declare_volume(result, provenance)

    # Within a genuine frame individual bars can still be unusable — a
    # row the source flagged, or one with no reading at all. Those bins
    # are marked here so the larger number cannot restore the trust its
    # constituents did not have. The column only appears when it has
    # something to say, so an all-clean fold is the plain six columns it
    # has always been.
    if provenance == indicators.GENUINE and not kept["_volume_usable"].all():
        result["volume_is_synthetic"] = ~kept["_volume_usable"]
    return result


def fifteen_minute(df: pd.DataFrame) -> pd.DataFrame:
    return fold(df, FIFTEEN_MIN)


def hourly(df: pd.DataFrame) -> pd.DataFrame:
    return fold(df, HOURLY)
