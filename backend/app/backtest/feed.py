"""The walk-forward feed — the only way a backtest sees its data.

Both engines already sliced correctly: `df.iloc[start : i + 1]` at bar `i`,
entry at the next bar's open. The problem was that nothing enforced it. The
correctness of every statistic this platform produces rested on a
convention, held in two places, that any future edit could break silently —
and look-ahead bias does not announce itself. It shows up as a strategy that
backtests beautifully and loses money, which is the most expensive failure
mode available.

So the frame is no longer passed around. A feed is, and it will not hand
over a bar the loop has not reached yet:

    feed = HistoricalFeed(candles)
    for i in feed.walk(warmup):
        window = feed.view(i)        # bars 0..i, never more
        fill   = feed.next_open(i)   # the next bar's OPEN, and nothing else

`next_open` is the one deliberate look into the future, and it is exactly
one number wide. A signal computed on a closed bar can realistically be
filled at the next bar's open; it cannot be filled at the next bar's *low*,
which is what an engine holding the whole frame could accidentally reach
for. Returning a float instead of a row makes that mistake unavailable
rather than merely discouraged.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..analytics import indicators

log = logging.getLogger(__name__)

DEFAULT_ANALYSIS_WINDOW = 300


class LookaheadError(RuntimeError):
    """Raised when something asks for data the walk has not reached.

    This is always a bug, never a data problem. It means a backtest was
    about to make a decision using information that did not exist at the
    time it claims to have made it.
    """


@dataclass
class CausalityReport:
    """Whether the indicators can see the future.

    Every indicator in this codebase is causal — exponential means, session
    cumulative sums, rolling windows. Nothing here fixes a leak; it detects
    one. The value is in the day somebody adds a z-score normalised over the
    whole frame, or a percentile rank, and every backtest silently starts
    scoring bars using prices from next week.
    """
    checked: int = 0
    columns: tuple[str, ...] = ()
    leaks: tuple[str, ...] = ()

    @property
    def causal(self) -> bool:
        return not self.leaks

    def to_dict(self) -> dict:
        return {"checked_cut_points": self.checked,
                "columns": list(self.columns),
                "causal": self.causal,
                "leaks": list(self.leaks)}


class HistoricalFeed:
    """Bars, handed out strictly in order.

    The cursor only moves forward, and only `walk` moves it. Everything else
    reads from it, so there is no way to reach bar `i + 5` while the loop
    believes it is at bar `i`.
    """

    def __init__(
        self,
        candles: pd.DataFrame,
        *,
        analysis_window: int = DEFAULT_ANALYSIS_WINDOW,
        tz: str = "Asia/Kolkata",
    ) -> None:
        # `indicators.enrich` calls `validate`, which sorts by timestamp, so
        # candles arriving newest-first are normalised rather than rejected.
        # There is deliberately no sortedness check here: it could never
        # fire, and a guard that cannot fire advertises a guarantee that is
        # actually being provided somewhere else.
        frame = indicators.enrich(candles)
        if frame["timestamp"].duplicated().any():
            raise ValueError(
                "duplicate timestamps in the candle frame — a backtest over "
                "these would trade the same bar twice")

        self._frame = frame
        self._ist = frame["timestamp"].dt.tz_convert(tz)
        self.analysis_window = analysis_window
        self._cursor = -1

    # ---- position -----------------------------------------------------

    def __len__(self) -> int:
        return len(self._frame)

    @property
    def cursor(self) -> int:
        """The furthest bar the walk has reached. -1 before it starts."""
        return self._cursor

    def walk(self, warmup: int = 60, reserve: int = 1) -> Iterator[int]:
        """Yield bar indices in order, advancing the cursor as it goes.

        `reserve` holds back bars at the end so `next_open` always has
        something to return. Stopping one bar short of the data is not a
        rounding detail: a signal on the final bar could never have been
        filled, and counting it would quietly inflate the trade count with
        trades that could not have happened.
        """
        last = len(self._frame) - reserve
        for i in range(warmup, last):
            self._cursor = i
            yield i

    def seek(self, index: int) -> None:
        """Move the cursor without iterating. For tests and for resuming.

        Rewinding is allowed; that is how a causality check re-runs a
        prefix. Skipping ahead is not the point of it, but it is the
        caller's explicit act either way.
        """
        if not -1 <= index < len(self._frame):
            raise IndexError(f"index {index} outside 0..{len(self._frame) - 1}")
        self._cursor = index

    # ---- reading ------------------------------------------------------

    def _guard(self, index: int, what: str) -> None:
        if index < 0:
            raise IndexError(f"{what}: negative index {index}")
        if index >= len(self._frame):
            raise IndexError(f"{what}: index {index} beyond {len(self._frame) - 1}")
        if index > self._cursor:
            raise LookaheadError(
                f"{what}: asked for bar {index} while the walk is at bar "
                f"{self._cursor}. That bar has not happened yet — a decision "
                "made from it would be a decision made with tomorrow's prices."
            )

    def bar(self, index: int) -> pd.Series:
        """One bar, at or before the cursor."""
        self._guard(index, "bar")
        return self._frame.iloc[index]

    def view(self, index: int) -> pd.DataFrame:
        """Everything the strategy is allowed to see at `index`.

        Capped at `analysis_window` bars. Structure detection is linear per
        call, so an uncapped window makes the whole backtest quadratic, and
        none of the checks look back further than a few sessions anyway.
        """
        self._guard(index, "view")
        start = max(0, index + 1 - self.analysis_window)
        return self._frame.iloc[start : index + 1]

    def timestamp(self, index: int) -> pd.Timestamp:
        self._guard(index, "timestamp")
        return self._frame["timestamp"].iloc[index]

    def ist(self, index: int) -> pd.Timestamp:
        """The bar's timestamp in IST, for session and trading-day logic."""
        self._guard(index, "ist")
        return self._ist.iloc[index]

    # ---- the one permitted look forward -------------------------------

    def next_open(self, index: int) -> float:
        """The open of the bar after `index`. A float, deliberately.

        This is the earliest price a signal computed on a closed bar could
        actually have been filled at. Returning the whole row instead would
        put that bar's high and low — which had not happened when the
        decision was made — one attribute access away.
        """
        self._guard(index, "next_open")
        nxt = index + 1
        if nxt >= len(self._frame):
            raise IndexError(
                f"no bar after {index}; walk() reserves the last bar so this "
                "cannot happen inside a normal loop")
        return float(self._frame["open"].iloc[nxt])

    def next_timestamp(self, index: int) -> pd.Timestamp:
        """When the fill happens. A clock reading, not market data."""
        self._guard(index, "next_timestamp")
        return self._frame["timestamp"].iloc[index + 1]

    # ---- self-checking -------------------------------------------------

    def verify_causality(self, samples: int = 8, seed: int = 0) -> CausalityReport:
        """Do the indicators give the same answer without the future?

        For a sample of cut points, recompute the indicator set from the
        prefix alone and compare the final row against the same row of the
        full-frame computation. They must match: an indicator that depends
        on bars after `i` is one that cannot be computed in real time, and a
        backtest using it is measuring hindsight.
        """
        columns = tuple(c for c in self._frame.columns
                        if c not in ("timestamp", "open", "high", "low", "close", "volume"))
        report = CausalityReport(columns=columns)
        if len(self._frame) < 60 or not columns:
            return report

        raw = self._frame[["timestamp", "open", "high", "low", "close", "volume"]]
        rng = np.random.default_rng(seed)
        cuts = sorted(set(rng.integers(50, len(self._frame), size=samples).tolist()))

        leaks: set[str] = set()
        for cut in cuts:
            prefix = indicators.enrich(raw.iloc[: cut + 1])
            full_row, prefix_row = self._frame.iloc[cut], prefix.iloc[-1]
            for column in columns:
                a, b = full_row[column], prefix_row[column]
                if pd.isna(a) and pd.isna(b):
                    continue
                if pd.isna(a) != pd.isna(b) or not np.isclose(a, b, rtol=1e-9, atol=1e-9):
                    leaks.add(column)
            report.checked += 1

        report.leaks = tuple(sorted(leaks))
        if leaks:
            log.error(
                "non-causal indicators detected: %s. Every backtest using "
                "them is scoring bars with information from later bars.",
                ", ".join(sorted(leaks)))
        return report

    # ---- escape hatch --------------------------------------------------

    def frame_for_reporting(self) -> pd.DataFrame:
        """The whole frame, for summarising a finished run.

        Never call this inside a walk. It exists so a completed backtest can
        report its own date range and bar count without the caller keeping a
        second reference to the data alongside the feed.
        """
        return self._frame
