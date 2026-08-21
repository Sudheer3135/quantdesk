"""Data-quality diagnostics.

The importer prevents bad rows from arriving. This inspects what actually
landed, which is a different question: guards can only reject what they were
written to recognise, and a dataset degrades in ways nobody anticipated —
a source starts publishing at four decimal places, a strike ladder narrows,
an OI feed resets at noon.

Design rule throughout: **a finding must be actionable and must not cry
wolf.** A diagnostic that reports every exchange holiday as missing data
gets ignored within a month, and then so does the one real outage. So
whole-session absences are cross-checked against the holiday calendar and
separated from mid-session holes, and anything the calendar cannot vouch for
is labelled `unverified` rather than asserted.

Findings carry a severity so a caller can act on the difference:

    error   — the data is wrong. Fix before trusting a backtest.
    warning — the data is suspicious, or thinner than it looks.
    info    — worth knowing, not worth acting on.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..market_calendar import is_provisional, is_session
from ..models import CandleRecord, OptionCandle, OptionContract
from . import oi_classifier
from . import option_coverage as option_coverage_lib

log = logging.getLogger(__name__)

# 09:15 to 15:30 inclusive is 375 minutes, so a full session holds 75
# five-minute bars — the 15:30 bar being the one that opens at 15:25.
SESSION_MINUTES = 375
TIMEFRAME_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

# NIFTY strikes are 50 points apart near the money.
STRIKE_STEP = 50.0

# Above this, an implied volatility is not a market view, it is a bad quote.
MAX_PLAUSIBLE_IV = 2.0
MIN_PLAUSIBLE_IV = 0.01


INDEX = "index"
OPTIONS = "options"


@dataclass
class Finding:
    check: str
    severity: str
    summary: str
    count: int = 0
    detail: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)
    # Which dataset this is a statement about. Index and option data are
    # collected by different mechanisms, fail in different ways, and are
    # fit for different purposes — the index archive backfills from Yahoo,
    # the option archive cannot be rebuilt at all. Rolling them into one
    # verdict meant a 22% option day marked complete index candles unusable.
    domain: str = INDEX

    def to_dict(self) -> dict:
        return asdict(self)


def expected_bars(timeframe: str) -> int:
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    return (SESSION_MINUTES // minutes) if minutes else 0


# ---------------------------------------------------------------- index

def missing_candles(db: Session, symbol: str, timeframe: str,
                    max_samples: int = 10) -> list[Finding]:
    """Sessions that are absent, and sessions that are incomplete.

    These are deliberately two findings rather than one. A wholly absent
    weekday is almost always an exchange holiday and needs a calendar to
    judge. A session that is present but short is a feed that dropped bars,
    which is a real problem and the one you want to see.
    """
    rows = db.execute(
        select(CandleRecord.session_date, func.count(CandleRecord.id))
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .group_by(CandleRecord.session_date)
        .order_by(CandleRecord.session_date)
    ).all()
    if not rows:
        return [Finding("missing_candles", "info", "Nothing stored yet.")]

    counts = {d: c for d, c in rows if d is not None}
    if not counts:
        return [Finding(
            "missing_candles", "warning",
            "Rows exist but none carry a session date — run the migrations.")]

    first, last = min(counts), max(counts)
    full = expected_bars(timeframe)
    findings: list[Finding] = []

    absent: list[str] = []
    unverified: list[str] = []
    day = first
    while day <= last:
        if day not in counts:
            state = is_session(day)
            if state is True:
                absent.append(day.isoformat())
            elif state is None:
                unverified.append(day.isoformat())
        day = day + timedelta(days=1)

    if absent:
        findings.append(Finding(
            "missing_sessions", "error",
            f"{len(absent)} trading day(s) inside the archived range have no "
            "candles at all. The market was open and nothing was captured.",
            count=len(absent), samples=absent[:max_samples]))

    if unverified:
        findings.append(Finding(
            "missing_sessions_unverified", "info",
            f"{len(unverified)} weekday(s) have no candles and fall in a year "
            "with no published holiday list, so they cannot be judged. Add "
            "the year to market_calendar.HOLIDAYS to resolve this.",
            count=len(unverified), samples=unverified[:max_samples]))

    if full:
        short = {d.isoformat(): c for d, c in counts.items() if c < full}
        # The most recent session is legitimately partial while it is still
        # being traded. Flagging it every afternoon is how a check becomes
        # background noise.
        short.pop(last.isoformat(), None)
        if short:
            worst = sorted(short.items(), key=lambda kv: kv[1])[:max_samples]
            findings.append(Finding(
                "incomplete_sessions", "warning",
                f"{len(short)} session(s) hold fewer than the {full} bars a "
                "full session should have. Bars were dropped mid-session.",
                count=len(short),
                detail={"expected_bars_per_session": full},
                samples=[{"session": d, "bars": c} for d, c in worst]))

    if any(is_provisional(d) for d in counts):
        findings.append(Finding(
            "calendar_provisional", "info",
            "Some sessions fall in a year whose holiday list is transcribed "
            "but unverified. Holiday-derived findings may be wrong.",
            count=sum(1 for d in counts if is_provisional(d))))

    return findings


def duplicate_candles(db: Session, symbol: str, timeframe: str) -> list[Finding]:
    """Bars sharing a timestamp.

    The unique constraint makes these impossible to create now. The check
    stays because it is cheap and because rows predating the constraint —
    or arriving through a future bulk load that bypasses the importer —
    would otherwise be invisible, and a duplicated bar is traded twice.
    """
    rows = db.execute(
        select(CandleRecord.timestamp, func.count(CandleRecord.id).label("n"))
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .group_by(CandleRecord.timestamp)
        .having(func.count(CandleRecord.id) > 1)
        .limit(20)
    ).all()
    if not rows:
        return []
    return [Finding(
        "duplicate_candles", "error",
        f"{len(rows)} timestamp(s) appear more than once. A backtest over "
        "these trades the same bar twice.",
        count=len(rows),
        samples=[{"timestamp": str(t), "rows": n} for t, n in rows])]


def impossible_prices(db: Session, symbol: str, timeframe: str) -> list[Finding]:
    """Bars that violate what a candle is.

    Not a judgement about whether a move was plausible — each of these is
    structurally impossible. The high is the highest price traded in the
    interval, so it cannot sit below the open, the close, or the low.
    """
    broken = db.scalars(
        select(CandleRecord)
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .where(
            (CandleRecord.high < CandleRecord.low)
            | (CandleRecord.high < CandleRecord.open)
            | (CandleRecord.high < CandleRecord.close)
            | (CandleRecord.low > CandleRecord.open)
            | (CandleRecord.low > CandleRecord.close)
            | (CandleRecord.low <= 0)
            | (CandleRecord.volume < 0)
        ).limit(20)
    ).all()
    if not broken:
        return []
    return [Finding(
        "impossible_prices", "error",
        f"{len(broken)} bar(s) are structurally impossible — a high below a "
        "low or a close outside the range. ATR and every stop derived from "
        "it are contaminated.",
        count=len(broken),
        samples=[{"timestamp": str(r.timestamp), "open": r.open, "high": r.high,
                  "low": r.low, "close": r.close} for r in broken[:5]])]


def price_jumps(db: Session, symbol: str, timeframe: str,
                threshold_pct: float = 5.0) -> list[Finding]:
    """Bar-to-bar moves too large to be a real intraday index move.

    A warning, never an error: NIFTY genuinely gaps, and calling a real gap
    corrupt would be worse than missing a bad tick.
    """
    rows = db.scalars(
        select(CandleRecord)
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .order_by(CandleRecord.timestamp)
    ).all()
    if len(rows) < 2:
        return []

    jumps = []
    for previous, current in zip(rows, rows[1:], strict=False):
        if previous.close <= 0:
            continue
        move = abs(current.close - previous.close) / previous.close * 100
        if move > threshold_pct:
            same_session = previous.session_date == current.session_date
            jumps.append({
                "from": str(previous.timestamp), "to": str(current.timestamp),
                "move_pct": round(move, 2),
                # An overnight gap is normal. A 5% move between two bars of
                # the same session is not.
                "intraday": bool(same_session),
            })

    intraday = [j for j in jumps if j["intraday"]]
    if not intraday:
        return []
    return [Finding(
        "price_jumps", "warning",
        f"{len(intraday)} intraday move(s) above {threshold_pct}% between "
        "consecutive bars. Usually a bad tick rather than a real move.",
        count=len(intraday), samples=intraday[:10])]


def synthetic_volume(db: Session, symbol: str, timeframe: str) -> list[Finding]:
    """Rows whose volume is a placeholder rather than a measurement.

    Worth a finding of its own because the consequence is invisible: the
    volume check silently contributes nothing to any signal, the weights
    renormalise around it, and the backtest looks entirely healthy while
    running a strategy with one fewer input than it claims.
    """
    total = db.scalar(
        select(func.count(CandleRecord.id))
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)) or 0
    if not total:
        return []

    fake = db.scalar(
        select(func.count(CandleRecord.id))
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe,
               CandleRecord.volume_is_synthetic.is_(True))) or 0
    if not fake:
        return []

    return [Finding(
        "synthetic_volume", "warning",
        f"{fake} of {total} rows ({fake / total:.0%}) carry placeholder "
        "volume. Yahoo publishes none for Indian index tickers. Every "
        "volume-based check is inactive on those bars.",
        count=fake, detail={"total_rows": total})]


def source_mix(db: Session, symbol: str, timeframe: str) -> list[Finding]:
    """Which sources the archive is made of, and whether any is untrustworthy."""
    rows = db.execute(
        select(CandleRecord.source, func.count(CandleRecord.id))
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .group_by(CandleRecord.source)
    ).all()
    if not rows:
        return []

    mix = {src or "unlabelled": count for src, count in rows}
    findings = [Finding("source_mix", "info", "Where the rows came from.",
                        count=sum(mix.values()), detail=mix)]

    if mix.get("mock"):
        findings.append(Finding(
            "mock_data_present", "error",
            f"{mix['mock']} rows came from the mock broker, which is a random "
            "walk. Any statistic computed over them describes noise.",
            count=mix["mock"]))
    if mix.get("unknown") or mix.get("unlabelled"):
        n = mix.get("unknown", 0) + mix.get("unlabelled", 0)
        findings.append(Finding(
            "unlabelled_source", "warning",
            f"{n} rows predate the required-source rule and cannot say where "
            "they came from.", count=n))
    return findings


# -------------------------------------------------------------- options

def option_bars_on_non_sessions(db: Session, underlying: str = "NIFTY") -> list[Finding]:
    """Option bars dated on a day the exchange never opened.

    NSE keeps serving the previous session's chain on a holiday — same
    strikes, same prices, HTTP 200 — so the collector filed a full day of
    identical snapshots for a day that never traded. The importer now
    refuses those on the chain's own timestamp, but rows captured before
    that guard existed are still in the archive, and option history cannot
    be rebuilt to replace them. Naming them is what lets a backtest exclude
    them.

    Weekends are included in the same count. They should be impossible —
    the collector's market-hours gate covers them — so finding any at all
    says something about the gate, not about the calendar.

    Years with no published holiday list are reported separately rather than
    guessed at. `market_calendar.is_session` answers None for those, and
    treating None as "not a session" would condemn a year of real data.
    """
    rows = db.execute(
        select(OptionCandle.session_date, func.count(OptionCandle.id))
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying,
               OptionCandle.session_date.isnot(None))
        .group_by(OptionCandle.session_date)
        .order_by(OptionCandle.session_date)
    ).all()
    if not rows:
        return []

    offending: list[tuple] = []
    unverified_years: set[int] = set()
    for session_date, count in rows:
        state = is_session(session_date)
        if state is None:
            unverified_years.add(session_date.year)
        elif state is False:
            offending.append((session_date, count))

    findings: list[Finding] = []
    if offending:
        bars = sum(count for _, count in offending)
        # `is_provisional` takes the date, not the year.
        provisional = sorted({d.year for d, _ in offending if is_provisional(d)})
        findings.append(Finding(
            "option_bars_on_non_sessions",
            # An error, not a warning: these bars are indistinguishable from
            # real ones by shape, and a backtest that reads them prices
            # trades against a chain that never moved.
            "error",
            f"{bars} option bar(s) are dated on {len(offending)} day(s) the "
            f"exchange did not trade. NSE replays the previous session's "
            f"chain when it is shut, so these repeat a close and are not "
            f"observations. Exclude them from any backtest."
            + (f" Note that {', '.join(map(str, provisional))} "
               f"{'is' if len(provisional) == 1 else 'are'} still provisional "
               f"in market_calendar — confirm against the NSE circular before "
               f"deleting anything." if provisional else ""),
            count=bars,
            detail={"days": len(offending),
                    "provisional_years": provisional},
            samples=[f"{d.isoformat()} ({c} bars)" for d, c in offending[:5]],
        ))

    if unverified_years:
        findings.append(Finding(
            "option_sessions_unverified", "info",
            f"No published holiday list for "
            f"{', '.join(map(str, sorted(unverified_years)))}, so option bars "
            f"in those years cannot be checked against the exchange calendar. "
            f"Add the year to market_calendar.HOLIDAYS to resolve this.",
            count=len(unverified_years),
            detail={"years": sorted(unverified_years)},
        ))

    return findings


def missing_option_strikes(db: Session, underlying: str = "NIFTY",
                           step: float = STRIKE_STEP) -> list[Finding]:
    """Holes in the strike ladder.

    Strikes are listed at a fixed interval, so a gap in the middle of the
    range means the capture missed rows — which matters because a backtest
    picking an at-the-money strike would silently land on a different one.
    Ladders are checked per expiry, and only between the lowest and highest
    strike actually seen: the ladder legitimately ends somewhere.
    """
    rows = db.execute(
        select(OptionContract.expiry_date, OptionContract.strike)
        .where(OptionContract.underlying == underlying)
        .distinct()
    ).all()
    if not rows:
        return []

    by_expiry: dict[date, set[float]] = defaultdict(set)
    for expiry, strike in rows:
        by_expiry[expiry].add(float(strike))

    holes = []
    for expiry, strikes in sorted(by_expiry.items()):
        if len(strikes) < 3:
            continue
        low, high = min(strikes), max(strikes)
        wanted = low
        while wanted <= high:
            if wanted not in strikes:
                holes.append({"expiry": expiry.isoformat(), "strike": wanted})
            wanted += step

    if not holes:
        return []
    return [Finding(
        "missing_option_strikes", "warning",
        f"{len(holes)} strike(s) are absent from the middle of a ladder. A "
        "strike selection may silently land on a different contract.",
        count=len(holes), samples=holes[:10])]


def abnormal_iv(db: Session, underlying: str = "NIFTY") -> list[Finding]:
    """Implied volatilities that are not market views.

    The unit trap is the one worth guarding: NSE publishes IV as a
    percentage and the pricing model takes a fraction. Storing 13.5 where
    0.135 belongs raises nothing and produces premiums roughly a hundred
    times too large, so a value above 200% is treated as a stored-unit bug
    rather than a volatile strike.
    """
    rows = db.execute(
        select(OptionCandle.id, OptionCandle.timestamp, OptionCandle.iv,
               OptionContract.strike, OptionContract.option_type)
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying, OptionCandle.iv.isnot(None))
    ).all()
    if not rows:
        return []

    absurd = [r for r in rows if r.iv > MAX_PLAUSIBLE_IV or r.iv <= 0]
    tiny = [r for r in rows if 0 < r.iv < MIN_PLAUSIBLE_IV]

    findings = []
    if absurd:
        findings.append(Finding(
            "abnormal_iv", "error",
            f"{len(absurd)} implied volatilities are outside 0–{MAX_PLAUSIBLE_IV:.0%}. "
            "Most likely stored as a percentage where a fraction is expected.",
            count=len(absurd),
            samples=[{"timestamp": str(r.timestamp), "strike": r.strike,
                      "type": r.option_type, "iv": r.iv} for r in absurd[:10]]))
    if tiny:
        findings.append(Finding(
            "near_zero_iv", "warning",
            f"{len(tiny)} implied volatilities are near zero, which usually "
            "means an untraded strike rather than a calm one.",
            count=len(tiny)))
    return findings


def _oi_changes(db: Session, underlying: str) -> list[oi_classifier.OIChange]:
    """Every consecutive pair of observations, with the context to judge it.

    Interval volume is derived here rather than stored: NSE publishes
    `totalTradedVolume` cumulatively for the session, so the contracts
    traded between two bars is the difference between them. That figure is
    what makes the physical test in `oi_classifier` possible at all.
    """
    rows = db.execute(
        select(OptionCandle.contract_id, OptionCandle.timestamp,
               OptionCandle.open_interest, OptionCandle.volume,
               OptionCandle.session_date, OptionCandle.underlying_close,
               OptionContract.expiry_date, OptionContract.strike,
               OptionContract.option_type)
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying,
               OptionCandle.open_interest.isnot(None))
        .order_by(OptionCandle.contract_id, OptionCandle.timestamp)
    ).all()
    if not rows:
        return []

    per_contract: dict[int, list] = defaultdict(list)
    for row in rows:
        per_contract[row.contract_id].append(row)

    changes: list[oi_classifier.OIChange] = []
    for series in per_contract.values():
        deltas = [abs(b.open_interest - a.open_interest)
                  for a, b in zip(series, series[1:], strict=False)]
        typical = sorted(deltas)[len(deltas) // 2] if deltas else 0.0

        for previous, current in zip(series, series[1:], strict=False):
            same_session = previous.session_date == current.session_date

            interval_volume = None
            if (same_session and current.volume is not None
                    and previous.volume is not None):
                interval_volume = current.volume - previous.volume

            moneyness = None
            if current.underlying_close:
                moneyness = ((current.strike - current.underlying_close)
                             / current.underlying_close * 100)

            changes.append(oi_classifier.OIChange(
                strike=current.strike,
                option_type=current.option_type,
                timestamp=str(current.timestamp),
                oi=current.open_interest,
                previous_oi=previous.open_interest,
                delta_oi=current.open_interest - previous.open_interest,
                interval_volume=interval_volume,
                typical_move=typical,
                observations=len(series),
                same_session=same_session,
                expiring=current.session_date == current.expiry_date,
                moneyness_pct=moneyness,
            ))
    return changes


def oi_discontinuities(db: Session, underlying: str = "NIFTY") -> list[Finding]:
    """Open interest that contradicts the volume that would have produced it.

    Classification lives in `data/oi_classifier.py`; the reasoning is in
    that module's docstring. In short: open interest can only move when
    contracts are traded, so a move larger than the interval's volume is
    arithmetically impossible, while a move of any size backed by matching
    volume is just a busy market.

    Every finding here is informational. Heavy trading at the money on
    expiry day is the most ordinary thing an option market does, and a
    check that can mark a dataset unusable for it would be worse than no
    check — so nothing in this function escalates above `warning`, and
    nothing it reports gates a backtest.
    """
    changes = _oi_changes(db, underlying)
    if not changes:
        return []

    buckets: dict[str, list] = defaultdict(list)
    for change in changes:
        verdict = oi_classifier.classify(change)
        if verdict.reportable:
            buckets[verdict.classification].append((change, verdict))

    def sample(entries, limit=10):
        return [{"timestamp": c.timestamp, "strike": c.strike,
                 "type": c.option_type, "delta_oi": c.delta_oi,
                 "interval_volume": c.interval_volume, "reason": v.reason}
                for c, v in entries[:limit]]

    findings: list[Finding] = []

    anomalies = buckets.get(oi_classifier.ANOMALY, [])
    if anomalies:
        findings.append(Finding(
            "oi_anomaly", "warning",
            f"{len(anomalies)} open-interest change(s) contradict the traded "
            "volume in the same interval — open interest cannot move without "
            "trades, so these are evidence of a data problem rather than of "
            "market activity.",
            count=len(anomalies),
            detail={"classification": oi_classifier.ANOMALY},
            samples=sample(anomalies)))

    busy = buckets.get(oi_classifier.HIGH_ACTIVITY, [])
    if busy:
        findings.append(Finding(
            "oi_high_activity", "info",
            f"{len(busy)} large open-interest move(s), each fully supported "
            "by trading volume. Normal market behaviour, reported for "
            "visibility rather than as a problem.",
            count=len(busy),
            detail={"classification": oi_classifier.HIGH_ACTIVITY},
            samples=sample(busy, 5)))

    unknown = buckets.get(oi_classifier.INSUFFICIENT_EVIDENCE, [])
    if unknown:
        findings.append(Finding(
            "oi_insufficient_evidence", "info",
            f"{len(unknown)} open-interest change(s) could not be judged — "
            "too few observations for the contract, too little liquidity, or "
            "a move below one lot. Not a finding about the data, a statement "
            "about what this check can currently see.",
            count=len(unknown),
            detail={"classification": oi_classifier.INSUFFICIENT_EVIDENCE},
            samples=sample(unknown, 5)))

    return findings


def option_snapshot_coverage(
    db: Session,
    underlying: str = "NIFTY",
    timeframe: str | None = None,
    poll_seconds: int | None = None,
    min_backtest_pct: float | None = None,
) -> list[Finding]:
    """Whether the collector actually ran, session by session.

    The one diagnostic whose absence has already cost data: on 17-Aug-2026
    collection stopped at 12:50 IST, index candles backfilled from Yahoo as
    normal, every other check stayed green, and nothing reported the
    two-hour-forty hole in an archive that cannot be rebuilt.

    Severity is graded because partial coverage and no coverage are
    different problems. A session missing a few polls is a warning; one
    below the minimum usable for backtesting is an error, and that
    threshold is configurable because nothing has derived it yet.
    """
    settings = get_settings()
    timeframe = timeframe or settings.watch_timeframe
    poll_seconds = poll_seconds or settings.option_snapshot_interval_seconds
    min_backtest_pct = (min_backtest_pct if min_backtest_pct is not None
                        else settings.option_coverage_min_backtest_pct)

    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if not minutes:
        return [Finding("option_snapshot_coverage", "info",
                        f"unknown timeframe {timeframe!r}; coverage not assessed")]

    rows = db.execute(
        select(OptionCandle.session_date, OptionCandle.timestamp,
               OptionContract.expiry_date, func.max(OptionCandle.samples))
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying,
               OptionCandle.timeframe == timeframe)
        .group_by(OptionCandle.session_date, OptionCandle.timestamp,
                  OptionContract.expiry_date)
    ).all()

    if not rows:
        return [Finding(
            "option_snapshot_coverage", "info",
            "No option snapshots stored, so collector coverage cannot be "
            "assessed. This is not a report that the collector was down — "
            "there is simply nothing to measure yet.")]

    # A bucket's poll count is the most any contract in it recorded: one
    # poll writes every strike, so the maximum is the number of polls that
    # landed there, while individual strikes come and go from the ladder.
    polls: dict[date, dict] = defaultdict(dict)
    per_expiry: dict[date, dict] = defaultdict(lambda: defaultdict(dict))
    for session_date, timestamp, expiry, samples in rows:
        if session_date is None or timestamp is None:
            continue
        moment = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
        current = polls[session_date].get(moment, 0)
        polls[session_date][moment] = max(current, samples or 0)
        per_expiry[expiry][session_date][moment] = max(
            per_expiry[expiry][session_date].get(moment, 0), samples or 0)

    observed_days = sorted(polls)
    sessions = option_coverage_lib.assessable_sessions(observed_days[0], observed_days[-1])

    assessments = [
        option_coverage_lib.assess_session(day, polls.get(day, {}), minutes, poll_seconds)
        for day in sessions
    ]
    if not assessments:
        return [Finding(
            "option_snapshot_coverage", "info",
            "Option snapshots exist but none fall on an assessable trading "
            "session — nothing to measure.")]

    by_severity: dict[str, list] = defaultdict(list)
    for assessment in assessments:
        by_severity[assessment.severity(min_backtest_pct)].append(assessment)

    findings: list[Finding] = []

    critical = by_severity.get("error", [])
    if critical:
        findings.append(Finding(
            "option_coverage_critical", "error",
            f"{len(critical)} trading session(s) hold less than "
            f"{min_backtest_pct:g}% of the option snapshots they should. "
            "Option history cannot be backfilled, so these gaps are "
            "permanent and any option backtest covering them is running on "
            "data that was never captured.",
            count=len(critical),
            detail={"min_backtest_coverage_pct": min_backtest_pct},
            samples=[a.to_dict() for a in critical[:10]]))

    partial = by_severity.get("warning", [])
    if partial:
        findings.append(Finding(
            "option_coverage_incomplete", "warning",
            f"{len(partial)} trading session(s) are missing some option "
            "snapshots. Usable, but the bars in those windows aggregate "
            "fewer observations than they should.",
            count=len(partial),
            samples=[a.to_dict() for a in partial[:10]]))

    # Per-expiry, because a ladder can roll mid-session and leave one
    # contract series far thinner than the session total suggests.
    expiry_rows = []
    for expiry, sessions_for_expiry in sorted(per_expiry.items()):
        expected = sum(option_coverage_lib.expected_polls_for(day, minutes, poll_seconds)
                       for day in sessions_for_expiry)
        seen = sum(sum(buckets.values()) for buckets in sessions_for_expiry.values())
        expiry_rows.append({
            "expiry": expiry.isoformat() if expiry else None,
            "sessions": len(sessions_for_expiry),
            "observed_polls": seen,
            "expected_polls": expected,
            "coverage_pct": round(min(100.0, seen / expected * 100), 1) if expected else 0.0,
        })

    total_expected = sum(a.expected_polls for a in assessments)
    total_observed = sum(a.observed_polls for a in assessments)
    findings.append(Finding(
        "option_snapshot_coverage", "info",
        f"{total_observed:,} of {total_expected:,} expected one-minute "
        f"snapshots captured across {len(assessments)} assessed session(s).",
        count=total_observed,
        detail={
            "coverage_pct": round(min(100.0, total_observed / total_expected * 100), 1)
            if total_expected else 0.0,
            "assessed_from": sessions[0].isoformat(),
            "assessed_to": sessions[-1].isoformat(),
            "poll_interval_seconds": poll_seconds,
            "bar_minutes": minutes,
            "by_expiry": expiry_rows,
            "note": (
                "Sessions outside the assessed window are not evaluated. "
                "Before the first stored snapshot there is no basis for "
                "saying whether the collector should have been running, and "
                "assuming it was would invent an outage."),
        },
        samples=[a.to_dict() for a in assessments[-5:]]))

    return findings


def option_coverage(db: Session, underlying: str = "NIFTY") -> list[Finding]:
    """How much option history exists — usually none, and that matters."""
    bars = db.scalar(
        select(func.count(OptionCandle.id))
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying)) or 0

    if not bars:
        return [Finding(
            "option_history_empty", "info",
            "No option history stored. There is no free source to backfill "
            "from — NSE publishes a live snapshot, not a tape — so this fills "
            "forward from the day the agent starts capturing. Until it does, "
            "option backtests price every trade with Black-Scholes at a "
            "constant IV.")]

    thin = db.scalar(
        select(func.count(OptionCandle.id))
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying,
               OptionCandle.samples <= 1)) or 0

    findings = [Finding(
        "option_history", "info", f"{bars} option bars stored.", count=bars)]
    if thin:
        findings.append(Finding(
            "single_sample_bars", "warning",
            f"{thin} of {bars} bars were built from a single snapshot, so "
            "their high and low equal their close. The range is not real and "
            "any statistic measuring intrabar movement will understate it.",
            count=thin))
    return findings


# ---------------------------------------------------------------- report

def index_coverage_pct(db: Session, symbol: str, timeframe: str) -> tuple[float, int, int]:
    """How complete the index archive is, as a percentage of expected bars.

    Measured over the trading sessions between the first and last stored
    candle. A session that is entirely absent counts against coverage —
    that is the point — while weekends and exchange holidays are excluded
    because nothing was ever expected on them.

    Per-session counts are capped at the expected bar count: Yahoo returns
    a 15:30 bar in addition to the 75 five-minute buckets, and letting that
    push a session past 100% would quietly offset a genuinely short day
    elsewhere.
    """
    rows = db.execute(
        select(CandleRecord.session_date, func.count(CandleRecord.id))
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .group_by(CandleRecord.session_date)
    ).all()
    counts = {d: c for d, c in rows if d is not None}
    if not counts:
        return 0.0, 0, 0

    per_session = expected_bars(timeframe)
    if not per_session:
        return 0.0, 0, 0

    sessions = [d for d in option_coverage_lib.assessable_sessions(
        min(counts), max(counts))]
    if not sessions:
        return 0.0, 0, 0

    expected = len(sessions) * per_session
    observed = sum(min(counts.get(day, 0), per_session) for day in sessions)
    return round(min(100.0, observed / expected * 100), 1), observed, expected


def _domain_verdict(errors: int, warnings: int) -> str:
    return "unusable" if errors else "usable with caveats" if warnings else "clean"


def report(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
           include_options: bool = True,
           index_min_backtest_pct: float | None = None,
           option_min_backtest_pct: float | None = None) -> dict:
    """Every diagnostic, worst first, reported separately per dataset.

    Index and option readiness are different questions with different
    answers, and merging them was actively misleading: a 22%-covered option
    day marked a complete, clean index archive as unusable. Nothing about
    the index candles had changed.

    The top-level verdict names both rather than collapsing to the worse of
    the two, so neither can hide behind the other.
    """
    settings = get_settings()
    index_min = (index_min_backtest_pct if index_min_backtest_pct is not None
                 else settings.index_coverage_min_backtest_pct)
    option_min = (option_min_backtest_pct if option_min_backtest_pct is not None
                  else settings.option_coverage_min_backtest_pct)

    findings: list[Finding] = []
    for finding in (missing_candles(db, symbol, timeframe)
                    + duplicate_candles(db, symbol, timeframe)
                    + impossible_prices(db, symbol, timeframe)
                    + price_jumps(db, symbol, timeframe)
                    + synthetic_volume(db, symbol, timeframe)
                    + source_mix(db, symbol, timeframe)):
        finding.domain = INDEX
        findings.append(finding)

    if include_options:
        for finding in (option_coverage(db, symbol)
                        + option_snapshot_coverage(db, symbol)
                        + option_bars_on_non_sessions(db, symbol)
                        + missing_option_strikes(db, symbol)
                        + abnormal_iv(db, symbol)
                        + oi_discontinuities(db, symbol)):
            finding.domain = OPTIONS
            findings.append(finding)

    rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (rank.get(f.severity, 3), -f.count))

    def block(domain: str, coverage: float, minimum: float) -> dict:
        mine = [f for f in findings if f.domain == domain]
        errors = sum(1 for f in mine if f.severity == "error")
        warnings = sum(1 for f in mine if f.severity == "warning")
        return {
            "verdict": _domain_verdict(errors, warnings),
            "coverage": coverage,
            "errors": errors,
            "warnings": warnings,
            # Two conditions, both necessary. No errors means nothing in the
            # data is known to be wrong; the coverage floor means there is
            # enough of it to measure anything with. A clean archive of four
            # sessions passes the first and fails the second.
            "backtest_eligible": errors == 0 and coverage >= minimum,
            "min_coverage_pct": minimum,
            "findings": [f.to_dict() for f in mine],
        }

    index_pct, index_observed, index_expected = index_coverage_pct(db, symbol, timeframe)
    index_block = block(INDEX, index_pct, index_min)
    index_block["observed_bars"] = index_observed
    index_block["expected_bars"] = index_expected

    option_pct = 0.0
    for finding in findings:
        if finding.check == "option_snapshot_coverage":
            option_pct = finding.detail.get("coverage_pct", 0.0)
    options_block = block(OPTIONS, option_pct, option_min) if include_options else None

    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")

    summary = f"index {index_block['verdict']}"
    if options_block:
        summary += f"; options {options_block['verdict']}"

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        # Names both. A single worst-of verdict would say "unusable" on a
        # day the index archive is complete, which is the exact confusion
        # this split exists to remove.
        "verdict": summary,
        "backtest_eligible": {
            "index": index_block["backtest_eligible"],
            "options": options_block["backtest_eligible"] if options_block else False,
        },
        "errors": errors,
        "warnings": warnings,
        "index": index_block,
        "findings": [f.to_dict() for f in findings],
    }
    if options_block:
        result["options"] = options_block
    return result
