"""The one gate every candle passes through before it is stored.

The guards themselves already existed and are good — `drop_unclosed`,
`drop_outside_session` and `drop_future` in `analytics/indicators.py` were
each written after a specific incident and none of them are reimplemented
here. What was missing is accounting.

Those functions drop rows and log. Logging is fine when a handful of bars
are discarded and useless when a source quietly changes format and 90% of a
backfill disappears: the import reports "written: 400", the number looks
plausible, and nothing anywhere says that 3,600 rows were thrown away. A
gate that cannot tell you what it rejected is indistinguishable from a bug.

So this module runs the same guards in sequence and counts what each one
removed, then adds the two checks that did not exist: exchange holidays and
structurally impossible bars.

**Attribution is first-match, not exhaustive.** A Sunday bar dated next year
is counted once, against whichever check runs first. The totals are exact;
the split between reasons is indicative.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..analytics.indicators import (
    GENUINE,
    UNKNOWN,
    drop_future,
    drop_outside_session,
    drop_unclosed,
    looks_like_placeholder,
    validate,
    volume_provenance,
)
from ..market_calendar import is_holiday

# Order matters: cheapest and most categorical first, so that a bar failing
# several checks is attributed to the most fundamental one.
REASONS = (
    "future_dated",
    "unclosed_bar",
    "outside_session",
    "exchange_holiday",
    "impossible_ohlc",
)


@dataclass
class RejectionReport:
    """What the gate threw away, and why."""
    rows_in: int = 0
    rows_out: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    volume_is_synthetic: bool = False
    unverified_calendar_years: list[int] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return self.rows_in - self.rows_out

    @property
    def rejection_rate(self) -> float:
        return (self.rejected / self.rows_in) if self.rows_in else 0.0

    def to_dict(self) -> dict:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rejected": self.rejected,
            "rejection_rate_pct": round(self.rejection_rate * 100, 2),
            "by_reason": {k: v for k, v in self.counts.items() if v},
            "samples": self.samples,
            "volume_is_synthetic": self.volume_is_synthetic,
            "unverified_calendar_years": self.unverified_calendar_years,
        }

    def warnings(self) -> list[str]:
        """Plain sentences worth surfacing to whoever ran the import."""
        out: list[str] = []
        if self.rows_in and self.rejection_rate > 0.5:
            out.append(
                f"{self.rejection_rate:.0%} of incoming rows were rejected. "
                "That is high enough to suspect the source changed format "
                "rather than that the data was bad."
            )
        if self.rows_out == 0 and self.rows_in > 0:
            out.append("Every row was rejected — nothing was stored.")
        if self.volume_is_synthetic:
            out.append(
                "Volume is a constant placeholder, not traded volume. "
                "Anything derived from it is unavailable, not neutral."
            )
        if self.unverified_calendar_years:
            years = ", ".join(str(y) for y in self.unverified_calendar_years)
            out.append(
                f"No verified NSE holiday list for {years}; holiday "
                "filtering fell back to weekends only for those dates."
            )
        return out


def impossible_mask(df: pd.DataFrame) -> pd.Series:
    """Bars that could not have happened, whatever the source claims.

    These are not judgement calls about whether a move was plausible. Each
    one is a violation of what a candle *is*: the high is the highest price
    traded in the interval, so it cannot sit below the open, the close or
    the low. A source returning these is malfunctioning, and storing them
    poisons ATR and every stop derived from it.
    """
    if df.empty:
        return pd.Series(dtype=bool)

    o, h, low, c = df["open"], df["high"], df["low"], df["close"]
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(low) & np.isfinite(c)
    positive = (o > 0) & (h > 0) & (low > 0) & (c > 0)
    ordered = (h >= low) & (h >= o) & (h >= c) & (low <= o) & (low <= c)
    volume_ok = df["volume"].fillna(0) >= 0

    return ~(finite & positive & ordered & volume_ok)


def holiday_mask(df: pd.DataFrame, tz: str = "Asia/Kolkata") -> tuple[pd.Series, list[int]]:
    """Bars dated on a published NSE holiday.

    Years with no holiday list are left alone and reported, never guessed
    at. See `market_calendar.is_holiday` for why the three-valued answer
    matters.
    """
    if df.empty:
        return pd.Series(dtype=bool), []

    days = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(tz).dt.date
    unverified: set[int] = set()
    flags = []
    for day in days:
        state = is_holiday(day)
        if state is None:
            unverified.add(day.year)
            flags.append(False)
        else:
            flags.append(state)
    return pd.Series(flags, index=df.index), sorted(unverified)


def _sample(df: pd.DataFrame, keep: pd.Series, limit: int = 3) -> list[str]:
    """A few example timestamps, so a count leads somewhere actionable."""
    bad = df.loc[~keep, "timestamp"]
    return [str(t) for t in bad.head(limit)]


def clean_candles(df: pd.DataFrame, timeframe: str) -> tuple[pd.DataFrame, RejectionReport]:
    """Run every guard, counting what each removes.

    Returns the surviving rows and a report. The caller decides what to do
    about a bad report — this function never raises on data quality, because
    a partially good backfill is still worth storing as long as the damage
    is visible.
    """
    report = RejectionReport(counts=dict.fromkeys(REASONS, 0))
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"]), report

    out = validate(df)
    report.rows_in = len(out)
    # The stored flag records what is known or suspected about these rows:
    # a source that declared a substitute, or a column that looks like one.
    # It is a demotion only — looking plausible never clears the flag, and
    # clearing it would not make the volume usable anyway, because that is
    # decided by the declared provenance.
    declared = volume_provenance(out)
    report.volume_is_synthetic = (declared != GENUINE
                                  if declared != UNKNOWN
                                  else looks_like_placeholder(out))

    # 1-3: the existing guards, measured rather than reimplemented.
    before = len(out)
    out = drop_future(out)
    report.counts["future_dated"] = before - len(out)

    before = len(out)
    out = drop_unclosed(out, timeframe)
    report.counts["unclosed_bar"] = before - len(out)

    before = len(out)
    out = drop_outside_session(out)
    report.counts["outside_session"] = before - len(out)

    # 4: holidays, which no existing guard knew about.
    holidays, unverified = holiday_mask(out)
    report.unverified_calendar_years = unverified
    if len(out):
        keep = ~holidays
        report.counts["exchange_holiday"] = int((~keep).sum())
        if report.counts["exchange_holiday"]:
            report.samples["exchange_holiday"] = _sample(out, keep)
        out = out[keep].reset_index(drop=True)

    # 5: bars that violate what a candle is.
    if len(out):
        keep = ~impossible_mask(out)
        report.counts["impossible_ohlc"] = int((~keep).sum())
        if report.counts["impossible_ohlc"]:
            report.samples["impossible_ohlc"] = _sample(out, keep)
        out = out[keep].reset_index(drop=True)

    report.rows_out = len(out)
    return out, report
