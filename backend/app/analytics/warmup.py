"""Declared warmup and declared history (TC-5).

Two different questions, and they used to be answered in three places that
disagreed.

  **When is a feature valid?** An EMA200 computed by pandas has a number on
  the very first bar — `ewm(adjust=False)` seeds itself from the first close
  — and that number was being read as a 200-bar trend on the sixty-first bar
  of a backtest. It is not one. A feature is available from the bar at which
  its declared warmup is met and not before; until then it is NaN, and
  every reader already treats NaN as unavailable.

  **How much history does a decision see?** The backtest cut each decision
  to a 300-bar window, the live agent handed the engine whatever five days
  the broker returned, and a full-history study used everything. An EMA is
  path dependent, so the same bar produced three different EMA200s — and
  three different signals — depending on who asked. Now both the live path
  and the replay trim to one declared history before computing anything,
  so the same inputs give the same answer wherever they are computed.

Both numbers live here and nowhere else. They are measurement settings, not
strategy parameters: nothing about them was chosen by looking at what they
do to a result, and they must not be tuned that way.
"""
from __future__ import annotations

import pandas as pd

# Bars of history a feature needs before its value means what its name
# says. An EMA of length N is declared valid from its Nth bar: the smallest
# history that has seen a full period, stated rather than tuned. ATR is a
# rolling mean of true range and needs its period too.
WARMUP_BARS: dict[str, int] = {
    "ema20": 20,
    "ema50": 50,
    "ema100": 100,
    "ema200": 200,
    "atr14": 14,
}

# How many completed bars every decision is computed from, live and in
# replay. Must be at least the longest warmup, or the longest feature could
# never become valid.
ANALYSIS_HISTORY_BARS = 300

if ANALYSIS_HISTORY_BARS < max(WARMUP_BARS.values()):
    raise AssertionError("declared history is shorter than the longest warmup")


def mask(df: pd.DataFrame) -> pd.DataFrame:
    """Blank every feature before its declared warmup is met. In place.

    Positional: the first `N - 1` rows of a feature with warmup `N` become
    NaN, so row `N - 1` — the Nth bar — is the first valid one.
    """
    for column, bars in WARMUP_BARS.items():
        if column in df.columns and bars > 1:
            df.loc[df.index[: bars - 1], column] = float("nan")
    return df


def declared_history(candles: pd.DataFrame) -> pd.DataFrame:
    """The last `ANALYSIS_HISTORY_BARS` bars, attributes preserved.

    The live path calls this before computing anything so that it sees the
    same window a replay of the same bar sees. More history is not more
    accuracy here; it is a different EMA.
    """
    if len(candles) <= ANALYSIS_HISTORY_BARS:
        return candles
    trimmed = candles.iloc[-ANALYSIS_HISTORY_BARS:].reset_index(drop=True)
    trimmed.attrs = dict(candles.attrs)
    return trimmed


def describe() -> dict:
    return {"warmup_bars": dict(WARMUP_BARS),
            "analysis_history_bars": ANALYSIS_HISTORY_BARS,
            "rule": "a feature is NaN until its declared warmup is met; live "
                    "and replay both trim to the declared history"}
