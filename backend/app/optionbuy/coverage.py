"""The gate that refuses a backtest rather than running a fictional one.

`/backtest/run` already returns 409 rather than quietly running on a shorter
window than you asked for, because a six-week result and a two-year result
look identical in the output and you would act on either. Option history
needs the same gate for a stronger reason: it cannot be backfilled at all.

NSE publishes a live snapshot of the chain, not a tape. A session the
collector missed is gone permanently — no vendor sells it back, and no
amount of waiting recovers it. So a run over a window with holes has exactly
two honest outcomes: refuse, or price the holes with a model and label every
one of those fills. It must never fill the hole with something that looks
like data.

What the policy changes:

  ``observed_only``     Every trading session in the window must clear the
                        coverage floor. Anything less is refused, with the
                        failing sessions named.
  ``prefer_observed``   The archive must hold something inside the window;
                        sessions it does not cover are named up front and
                        will be priced by the model and labelled MODELLED.
  ``modelled_only``     The archive is not consulted. Passes, and says in
                        the report that every fill in the run will be a
                        calculation rather than a price.

There is a second gate after the run, in `strategy.py`: a caller can demand
that some share of actual fills came from the archive, and a run that misses
it is returned as a refusal rather than as statistics. Coverage measured
before the walk is about sessions; evidence measured after it is about the
trades that were actually taken, and a window can pass the first and fail
the second when every trade lands in the uncovered part.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..data import option_coverage, repository
from ..market_calendar import sessions_between
from ..market_hours import IST
from .chain import ChainStore
from .pricing import MODELLED_ONLY, OBSERVED_ONLY, POLICIES, PREFER_OBSERVED

# The share of a session's expected polls that must be stored before that
# session counts as covered. Inherited from `data/option_coverage.py` rather
# than restated, so the platform has one definition of "enough".
DEFAULT_MIN_SESSION_COVERAGE_PCT = option_coverage.DEFAULT_MIN_BACKTEST_COVERAGE_PCT

# Below this many covered sessions, nothing computed from the run is
# evidence of anything. It is the same floor the index backtest applies.
DEFAULT_MIN_SESSIONS = 5


@dataclass
class SessionCoverage:
    session: str
    coverage_pct: float
    observed_polls: int
    expected_polls: int
    covered: bool
    contracts: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CoverageReport:
    """Whether the run may proceed, and the complete reason either way."""
    ok: bool
    policy: str
    reason: str | None = None
    index: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    requirements: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fix: str | None = None

    def to_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "pricing_policy": self.policy,
            "requirements": self.requirements,
            "index": self.index,
            "options": self.options,
            "warnings": self.warnings,
        }
        if not self.ok:
            out["error"] = "insufficient option coverage"
            out["reason"] = self.reason
            out["fix"] = self.fix
        return out


def _window(store: ChainStore, start: date | None, end: date | None,
            index_first: datetime | None, index_last: datetime | None
            ) -> tuple[date | None, date | None]:
    """The calendar span the run actually covers, in IST session dates.

    Bounded by the index candles rather than by the request, because a run
    cannot trade a session it has no bars for — grading option coverage on
    days the strategy could never have looked at would report failures that
    change nothing.
    """
    first = start or (index_first.astimezone(IST).date() if index_first else None)
    last = end or (index_last.astimezone(IST).date() if index_last else None)
    return first, last


def assess_options(store: ChainStore, first: date, last: date,
                   *, min_session_coverage_pct: float) -> list[SessionCoverage]:
    """Per-session option coverage across the window.

    Every trading session in the span is graded, including the ones the
    archive holds nothing for. Assessing only the sessions with data would
    grade the collector on the days it ran, which is the measurement that
    made a two-hour-forty outage invisible in the first place.
    """
    sessions, _unverified = sessions_between(first, last)
    stored = store.sessions()

    out: list[SessionCoverage] = []
    for day in sessions:
        polls = store.polls_by_bucket(day) if day in stored else {}
        assessed = option_coverage.assess_session(day, polls)
        out.append(SessionCoverage(
            session=day.isoformat(),
            coverage_pct=round(assessed.coverage_pct, 1),
            observed_polls=assessed.observed_polls,
            expected_polls=assessed.expected_polls,
            covered=assessed.coverage_pct >= min_session_coverage_pct,
            contracts=store.contracts_on(day),
        ))
    return out


def gate(
    db: Session,
    *,
    symbol: str = "NIFTY",
    timeframe: str = "5m",
    start: date | None = None,
    end: date | None = None,
    policy: str = PREFER_OBSERVED,
    store: ChainStore | None = None,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    min_session_coverage_pct: float = DEFAULT_MIN_SESSION_COVERAGE_PCT,
) -> CoverageReport:
    """May this option backtest run, and on what evidence?

    Index coverage is checked first and for every policy: a modelled run
    still needs the bars the signals are computed from, and refusing early
    keeps a missing-candles problem from being reported as an option problem.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown pricing policy {policy!r}")

    requirements = {
        "min_sessions": min_sessions,
        "min_session_coverage_pct": min_session_coverage_pct,
        "policy_requires_observed_prices": policy != MODELLED_ONLY,
        "every_session_must_be_covered": policy == OBSERVED_ONLY,
    }

    gap = repository.check_coverage(db, symbol, timeframe, start=start, end=end,
                                    min_sessions=min_sessions)
    if gap is not None:
        return CoverageReport(
            ok=False, policy=policy,
            reason="the index archive cannot serve this window: " + gap.reason,
            index=gap.to_dict(), requirements=requirements,
            fix=gap.to_dict().get("fix"))

    index = repository.coverage(db, symbol, timeframe)
    index_block = index.to_dict()

    if policy == MODELLED_ONLY:
        return CoverageReport(
            ok=True, policy=policy, index=index_block,
            requirements=requirements,
            options={
                "consulted": False,
                "note": "The option archive was not read. Every premium in "
                        "this run is Black-Scholes at a constant IV, labelled "
                        "MODELLED, and no fill in it is evidence of a price "
                        "anyone could have traded at.",
            },
            warnings=["Every fill is MODELLED. Read this run as a study of "
                      "the model, not of the market."])

    if store is None:
        raise ValueError("a chain store is required for any policy that reads "
                         "observed prices")

    first, last = _window(store, start, end, index.first, index.last)
    if first is None or last is None:
        return CoverageReport(
            ok=False, policy=policy,
            reason="no index candles, so there is no window to assess",
            index=index_block, requirements=requirements,
            fix="POST /data/import/index to seed the archive.")

    per_session = assess_options(store, first, last,
                                 min_session_coverage_pct=min_session_coverage_pct)
    covered = [s for s in per_session if s.covered]
    missing = [s for s in per_session if not s.covered]
    fingerprint = store.fingerprint()

    options_block = {
        "consulted": True,
        "window": {"first": first.isoformat(), "last": last.isoformat()},
        "sessions_in_window": len(per_session),
        "sessions_covered": len(covered),
        "sessions_missing": len(missing),
        "coverage_floor_pct": min_session_coverage_pct,
        "per_session": [s.to_dict() for s in per_session],
        "archive": fingerprint,
    }

    fix = (
        "Option history cannot be backfilled — NSE publishes a snapshot, not "
        "a tape. Either narrow the window to the sessions the collector "
        "covered, run with pricing_policy=prefer_observed and read the "
        "MODELLED share in the result, or leave the collector running and "
        "come back to this window later."
    )

    if store.empty:
        return CoverageReport(
            ok=False, policy=policy,
            reason="the option archive holds no bars at all for this window",
            index=index_block, options=options_block,
            requirements=requirements, fix=fix)

    if policy == OBSERVED_ONLY:
        if missing:
            names = ", ".join(s.session for s in missing[:8])
            more = f" and {len(missing) - 8} more" if len(missing) > 8 else ""
            return CoverageReport(
                ok=False, policy=policy,
                reason=(f"{len(missing)} of {len(per_session)} sessions fall "
                        f"below the {min_session_coverage_pct}% option coverage "
                        f"floor: {names}{more}. The observed_only policy will "
                        "not model through them."),
                index=index_block, options=options_block,
                requirements=requirements, fix=fix)
        if len(covered) < min_sessions:
            return CoverageReport(
                ok=False, policy=policy,
                reason=(f"only {len(covered)} covered option session(s); at "
                        f"least {min_sessions} are needed before any statistic "
                        "means anything"),
                index=index_block, options=options_block,
                requirements=requirements, fix=fix)
        return CoverageReport(ok=True, policy=policy, index=index_block,
                              options=options_block, requirements=requirements)

    # prefer_observed
    warnings: list[str] = []
    if missing:
        warnings.append(
            f"{len(missing)} of {len(per_session)} sessions in this window are "
            f"below the {min_session_coverage_pct}% option coverage floor. "
            "Trades taken on them will be priced by the model and labelled "
            "MODELLED — they are assumptions, not fills.")
    if len(covered) < min_sessions:
        warnings.append(
            f"Only {len(covered)} session(s) clear the coverage floor, under "
            f"the {min_sessions} this platform treats as a minimum sample. "
            "The OBSERVED part of this result is too small to conclude from.")

    return CoverageReport(ok=True, policy=policy, index=index_block,
                          options=options_block, requirements=requirements,
                          warnings=warnings)


@dataclass
class EvidenceGate:
    """The second gate: what the fills actually turned out to be.

    Coverage is a claim about sessions. This is a claim about trades, and
    they come apart — a window can clear the floor on average while every
    trade the strategy took landed in the uncovered part of it. Asking for
    `min_observed_pct` is how a caller says "I want a result about the
    market, not about Black-Scholes", and a run that cannot deliver that is
    refused rather than footnoted.
    """
    required_pct: float
    achieved_pct: float
    trades: int

    @property
    def ok(self) -> bool:
        return self.required_pct <= 0 or self.achieved_pct >= self.required_pct

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "required_observed_pct": self.required_pct,
            "achieved_observed_pct": self.achieved_pct,
            "trades": self.trades,
        }

    def refusal(self) -> dict:
        return {
            "error": "insufficient observed pricing",
            "reason": (
                f"{self.achieved_pct:.1f}% of the {self.trades} fill(s) in this "
                f"run came from the option archive, below the "
                f"{self.required_pct:.1f}% this request required. The rest were "
                "modelled, and a statistic computed mostly from Black-Scholes "
                "describes the model rather than the market."),
            "evidence": self.to_dict(),
            "fix": "Lower min_observed_pct to accept modelled fills and read "
                   "the evidence breakdown, or narrow the window to sessions "
                   "the option collector actually covered.",
        }
