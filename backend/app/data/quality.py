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
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..market_calendar import is_provisional, is_session
from ..models import CandleRecord, OptionCandle, OptionContract

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


@dataclass
class Finding:
    check: str
    severity: str
    summary: str
    count: int = 0
    detail: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)

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


def oi_discontinuities(db: Session, underlying: str = "NIFTY",
                       jump_multiple: float = 10.0) -> list[Finding]:
    """Open interest that moves in ways open interest does not move.

    OI is a stock, not a flow: it accumulates and decays across a session.
    A mid-session reset to zero, or a single-bar change many times the
    typical one, means a dropped or malformed capture rather than a real
    positioning shift.

    Expiry boundaries are excluded — OI genuinely collapses to nothing when
    a contract expires, and flagging that would be flagging the calendar.
    """
    rows = db.execute(
        select(OptionCandle.contract_id, OptionCandle.timestamp,
               OptionCandle.open_interest, OptionCandle.session_date,
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

    # Negative open interest is checked over every row, not inside the
    # pairwise loop below. A contract with a single stored bar has no pairs,
    # so a pairwise-only check would silently never examine the first bar of
    # any contract — which, on a young archive, is most of them.
    negatives = [
        {"timestamp": str(row.timestamp), "strike": row.strike,
         "type": row.option_type, "open_interest": row.open_interest}
        for row in rows if row.open_interest < 0
    ]

    resets, jumps = [], []
    for series in per_contract.values():
        deltas = []
        for previous, current in zip(series, series[1:], strict=False):
            if current.open_interest < 0 or previous.open_interest < 0:
                continue
            same_session = previous.session_date == current.session_date
            expiring = current.session_date == current.expiry_date
            if (same_session and not expiring
                    and previous.open_interest > 0 and current.open_interest == 0):
                resets.append({"timestamp": str(current.timestamp),
                               "strike": current.strike,
                               "type": current.option_type})
            deltas.append((abs(current.open_interest - previous.open_interest),
                           current, expiring))

        if len(deltas) < 5:
            continue
        magnitudes = sorted(d[0] for d in deltas)
        median = magnitudes[len(magnitudes) // 2]
        if median <= 0:
            continue
        for magnitude, current, expiring in deltas:
            if not expiring and magnitude > median * jump_multiple:
                jumps.append({"timestamp": str(current.timestamp),
                              "strike": current.strike,
                              "type": current.option_type,
                              "change": magnitude})

    findings = []
    if negatives:
        findings.append(Finding(
            "negative_open_interest", "error",
            f"{len(negatives)} bar(s) hold negative open interest, which "
            "cannot happen.", count=len(negatives), samples=negatives[:10]))
    if resets:
        findings.append(Finding(
            "oi_reset_mid_session", "error",
            f"{len(resets)} contract-bars drop to zero open interest mid-session "
            "without expiring. That is a dropped capture, not a market event.",
            count=len(resets), samples=resets[:10]))
    if jumps:
        findings.append(Finding(
            "oi_discontinuity", "warning",
            f"{len(jumps)} open-interest change(s) exceed {jump_multiple:g}× the "
            "typical bar-to-bar change for that contract.",
            count=len(jumps), samples=jumps[:10]))
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

def report(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
           include_options: bool = True) -> dict:
    """Every diagnostic, worst first."""
    findings: list[Finding] = []
    findings += missing_candles(db, symbol, timeframe)
    findings += duplicate_candles(db, symbol, timeframe)
    findings += impossible_prices(db, symbol, timeframe)
    findings += price_jumps(db, symbol, timeframe)
    findings += synthetic_volume(db, symbol, timeframe)
    findings += source_mix(db, symbol, timeframe)

    if include_options:
        findings += option_coverage(db, symbol)
        findings += missing_option_strikes(db, symbol)
        findings += abnormal_iv(db, symbol)
        findings += oi_discontinuities(db, symbol)

    rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (rank.get(f.severity, 3), -f.count))

    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "verdict": (
            "unusable" if errors else
            "usable with caveats" if warnings else
            "clean"
        ),
        "errors": errors,
        "warnings": warnings,
        "findings": [f.to_dict() for f in findings],
    }
