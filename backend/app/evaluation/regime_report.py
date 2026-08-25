"""How the existing strategy performed in each market condition.

The point of the whole regime layer. The outcome study said the signals lose
0.66R on average; averaged over every condition the market can be in, that
number is the sum of things that should never have been added together. A
rule that works in a trend and fails in chop, measured across both, looks
like a rule that does not work.

So this joins each evaluated signal to the regime its bar was formed in and
reports the same statistics per condition. It computes no new statistic of
its own: the buckets come from `outcomes.group_by`, so "win rate" here is
the same function as "win rate" in the headline study and cannot drift from
it.

Three honesty constraints, all of which change how the output reads:

  The join is on the signal's own bar, not the exit bar. Splitting outcomes
  by the regime that was visible *after* the trade resolved would be a
  look-ahead dressed up as an analysis, and it would look like a very good
  one.

  A bar with no stored regime is reported as `unclassified` rather than
  dropped. Silently discarding unmatched rows would shrink the sample
  without saying so, and the reader would compare a 140-signal split against
  a 167-signal headline without noticing.

  Bucket sizes are small. A five-way split of 167 observations that are
  themselves not independent leaves some cells with a handful of trades, and
  `interpretation` says so per bucket rather than leaving a 100% win rate on
  n=2 to be read as a discovery.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from ..analytics import regime as regime_engine
from ..data import regime_store
from . import outcomes as outcome_study

log = logging.getLogger(__name__)

UNCLASSIFIED = "unclassified"

# Below this many resolved trades a bucket's win rate is noise. Not a filter
# — nothing is hidden — but every bucket carries the judgement so a reader
# does not have to supply it.
MIN_MEANINGFUL = 20


@dataclass
class RegimeReport:
    symbol: str
    timeframe: str
    selection: dict
    candles: dict
    regime_coverage: dict
    matched: int = 0
    unmatched: int = 0
    overall: dict = field(default_factory=dict)
    by_day_regime: list[dict] = field(default_factory=list)
    by_hour_regime: list[dict] = field(default_factory=list)
    by_day_regime_and_direction: list[dict] = field(default_factory=list)
    agreement: list[dict] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


CAVEATS = [
    "Split of an existing study, not a new one. Every outcome here is the "
    "same replay reported by /signals/outcomes, bucketed differently.",
    "Each signal is matched to the regime of the bar it was computed on — "
    "the last bar that had closed when the signal fired. Matching on the "
    "exit bar instead would let the future into the split and would flatter "
    "whichever regime the winners happened to end in.",
    "The classifier is causal: every feature at a bar uses that bar and "
    "earlier ones only. A prefix of the archive classifies its bars "
    "identically to the whole archive, which is what makes this split "
    "legitimate rather than circular.",
    "Regime thresholds were set from the shape of NIFTY 5-minute data, not "
    "fitted to these outcomes. Tuning them against the same 167 signals they "
    "are used to explain would manufacture the pattern it then reported.",
    "Bucket sizes are small and the observations are not independent — "
    "consecutive signals within one move describe that move, not separate "
    "evidence. Read `n` before any win rate.",
    "R multiples are gross, for the reason given in the outcome study: the "
    "cost model prices index points as if they were option premium.",
]


def _verdict(bucket: dict) -> str:
    """A plain sentence about whether a bucket's numbers mean anything."""
    n = bucket.get("resolved") or 0
    if n == 0:
        return "No resolved trades in this condition."
    if n < MIN_MEANINGFUL:
        return (f"Only {n} resolved trade(s) — too few to read as a result. "
                "Reported so the sample is visible, not as a finding.")
    return f"{n} resolved trades — enough to be worth a second look."


def _annotate(buckets: list[dict]) -> list[dict]:
    return [b | {"interpretation": _verdict(b)} for b in buckets]


def build(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
          quantity: int = outcome_study.EVALUATION_QUANTITY) -> RegimeReport:
    """Split the evaluated signals by the regime they were formed in."""
    picked = outcome_study.collect(db, symbol, timeframe, quantity=quantity)
    rows = picked.outcomes

    regimes = regime_store.load(db, symbol, timeframe)
    coverage = regime_store.coverage(db, symbol, timeframe)

    # Timestamp -> labels. An exact key match: `signal_bar_time` is a stored
    # bar's own timestamp, and the regime table is keyed on the same bars, so
    # there is nothing to round or search for. A nearest-match join here
    # would quietly paper over a regime table that is out of date.
    lookup: dict[str, tuple[str, str]] = {}
    if not regimes.empty:
        for stamp, day, hour in zip(regimes["timestamp"], regimes["day_regime"],
                                    regimes["hour_regime"], strict=True):
            lookup[pd.Timestamp(stamp).isoformat()] = (day, hour)

    # Tagged once, keyed on the signal's primary key, then read by each
    # grouping below. Doing the lookup inside the group functions instead
    # would re-run it four times and leave `matched` counting every pass.
    tagged = {row.signal_id: lookup.get(
        pd.Timestamp(row.signal_bar_time).isoformat(),
        (UNCLASSIFIED, UNCLASSIFIED)) for row in rows}
    matched = sum(1 for pair in tagged.values() if pair[0] != UNCLASSIFIED)

    def day_of(row) -> str:
        return tagged[row.signal_id][0]

    def hour_of(row) -> str:
        return tagged[row.signal_id][1]

    report = RegimeReport(
        symbol=symbol, timeframe=timeframe,
        selection=picked.report.to_dict(),
        candles=outcome_study.candle_span(picked.candles),
        regime_coverage=coverage,
        matched=matched,
        unmatched=len(rows) - matched,
        overall=outcome_study.bucket("all", rows).to_dict(),
        by_day_regime=_annotate(outcome_study.group_by(rows, day_of)),
        by_hour_regime=_annotate(outcome_study.group_by(rows, hour_of)),
        by_day_regime_and_direction=_annotate(outcome_study.group_by(
            rows, lambda r: f"{day_of(r)} / {r.action}")),
        # Where the two levels disagree is the interesting case: an hour
        # going against the day is either the turn or a trap, and the desk
        # currently has no rule that distinguishes them.
        agreement=_annotate(outcome_study.group_by(
            rows, lambda r: ("agree" if day_of(r) == hour_of(r) else "disagree"))),
        caveats=CAVEATS,
    )

    if regimes.empty:
        report.caveats = [
            "No regimes are stored, so every signal is reported as "
            "unclassified. Run POST /data/regimes/backfill first.",
            *CAVEATS]
    elif coverage.get("mixed_versions"):
        report.caveats = [
            "The regime table holds more than one engine version. This split "
            "mixes two definitions of the same label and should not be read "
            f"until it is re-classified with {regime_engine.ENGINE_VERSION}.",
            *CAVEATS]

    return report
