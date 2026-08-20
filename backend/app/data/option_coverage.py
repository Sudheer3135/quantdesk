"""Did the option collector actually run?

Option history cannot be backfilled. NSE publishes a live snapshot rather
than a tape, so a minute not captured is a minute gone permanently — and
nothing else in this system notices. On 17-Aug-2026 collection stopped at
12:50 IST and ran no further; index candles filled in from Yahoo as usual,
every other diagnostic stayed green, and the two-hour-forty hole in the
option archive was found by hand.

That is the failure this module exists to make impossible to miss.

**What is actually stored, and what that means for counting.** The collector
polls every 60 seconds but persists into 5-minute buckets, folding each
poll into the bucket it lands in and counting them in `samples`. So there
are two distinct failures, and conflating them hides the smaller one:

    a bucket that does not exist      → the collector was down
    a bucket with too few samples     → the collector ran but missed polls

Both are reported. Coverage is expressed in *polls* — observed samples over
the number of one-minute snapshots the session should have produced —
because that is the resolution at which data was actually lost.

**What this module will not do.** It will not assume the collector was
running. A session with no option data at all could mean an outage or
simply that collection had not started yet, and those are different facts.
Sessions are therefore only assessed inside the window bounded by the first
and last snapshot in the archive; anything outside it is reported as
un-assessed rather than as complete or as missing.

NSE runs continuously from 09:15 to 15:30 IST with no lunch break, unlike
exchanges that pause mid-day. `MARKET_PAUSES` exists so that if one is ever
introduced the expected-bucket calculation excludes it rather than
reporting the pause as an outage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from ..market_calendar import is_session
from ..market_hours import IST, MARKET_CLOSE, MARKET_OPEN

# Intraday halts, as (start, end) IST times. Empty for NSE: the equity and
# F&O segments trade straight through. A trading halt (circuit breaker) is
# not listed here — those are unscheduled, and treating an unscheduled halt
# as "expected missing data" would hide a real outage on the same day.
MARKET_PAUSES: list[tuple[time, time]] = []

# Where a gap sits changes what it means, so they are named rather than
# lumped together. A late start and an early stop are collector lifecycle
# problems; a hole in the middle is usually the source or the network.
LATE_START = "collector_started_late"
EARLY_STOP = "collector_stopped_early"
MID_SESSION = "mid_session_outage"
WHOLE_SESSION = "no_data_for_session"

# Coverage below this is treated as unusable for backtesting. It is a
# placeholder pending an agreed figure, not a derived one — no analysis
# says 90% is the line. Override it rather than trusting it.
DEFAULT_MIN_BACKTEST_COVERAGE_PCT = 90.0


@dataclass
class Gap:
    """A contiguous run of buckets that hold no snapshot."""
    kind: str
    start_ist: str
    end_ist: str
    minutes: int
    missing_buckets: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionCoverage:
    session_date: str
    expected_buckets: int
    observed_buckets: int
    expected_polls: int
    observed_polls: int
    gaps: list[Gap] = field(default_factory=list)
    first_ist: str | None = None
    last_ist: str | None = None
    under_sampled_buckets: int = 0

    @property
    def coverage_pct(self) -> float:
        if not self.expected_polls:
            return 0.0
        return min(100.0, self.observed_polls / self.expected_polls * 100)

    @property
    def bucket_coverage_pct(self) -> float:
        if not self.expected_buckets:
            return 0.0
        return min(100.0, self.observed_buckets / self.expected_buckets * 100)

    @property
    def missing_minutes(self) -> int:
        return sum(g.minutes for g in self.gaps)

    def severity(self, min_backtest_pct: float = DEFAULT_MIN_BACKTEST_COVERAGE_PCT,
                 complete_pct: float = 99.0) -> str:
        """`error` only below the backtesting minimum; partial is a warning.

        Complete coverage is deliberately not 100%: a poll landing a second
        late slips into the next bucket, so a flawless session routinely
        lands a fraction under. Calling that an incident is how a check
        becomes noise.
        """
        if self.observed_polls == 0:
            return "error"
        if self.coverage_pct < min_backtest_pct:
            return "error"
        if self.coverage_pct < complete_pct:
            return "warning"
        return "info"

    def to_dict(self) -> dict:
        return {
            "session_date": self.session_date,
            "coverage_pct": round(self.coverage_pct, 1),
            "bucket_coverage_pct": round(self.bucket_coverage_pct, 1),
            "expected_polls": self.expected_polls,
            "observed_polls": self.observed_polls,
            "expected_buckets": self.expected_buckets,
            "observed_buckets": self.observed_buckets,
            "under_sampled_buckets": self.under_sampled_buckets,
            "missing_minutes": self.missing_minutes,
            "first_ist": self.first_ist,
            "last_ist": self.last_ist,
            "gaps": [g.to_dict() for g in self.gaps],
        }


def _in_pause(moment: time) -> bool:
    return any(start <= moment < end for start, end in MARKET_PAUSES)


def expected_bucket_starts(session_date: date, timeframe_minutes: int = 5) -> list[datetime]:
    """Every bucket the collector should have written, in UTC.

    A bucket is expected when it *starts* inside the session. The bucket
    beginning exactly at the close is excluded: only a poll fired at
    precisely 15:30:00 could land in it, so counting it would report a
    one-bucket gap at the end of almost every otherwise perfect session.
    """
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")

    open_at = datetime.combine(session_date, MARKET_OPEN, tzinfo=IST)
    close_at = datetime.combine(session_date, MARKET_CLOSE, tzinfo=IST)

    buckets: list[datetime] = []
    moment = open_at
    while moment < close_at:
        if not _in_pause(moment.timetz().replace(tzinfo=None)):
            buckets.append(moment.astimezone(UTC))
        moment += timedelta(minutes=timeframe_minutes)
    return buckets


def expected_polls_for(session_date: date, timeframe_minutes: int = 5,
                       poll_seconds: int = 60) -> int:
    """One-minute snapshots the session should have produced."""
    per_bucket = max(1, int((timeframe_minutes * 60) // max(1, poll_seconds)))
    return len(expected_bucket_starts(session_date, timeframe_minutes)) * per_bucket


def find_gaps(expected: list[datetime], observed: set[datetime],
              timeframe_minutes: int = 5) -> list[Gap]:
    """Contiguous runs of expected buckets with nothing stored in them.

    Runs touching the start or end of the session are named differently:
    those are the collector starting late or stopping early, which points
    at process lifecycle rather than at the data source.
    """
    if not expected:
        return []

    gaps: list[Gap] = []
    run: list[datetime] = []

    def close_run() -> None:
        if not run:
            return
        touches_start = run[0] == expected[0]
        touches_end = run[-1] == expected[-1]
        if touches_start and touches_end:
            kind = WHOLE_SESSION
        elif touches_start:
            kind = LATE_START
        elif touches_end:
            kind = EARLY_STOP
        else:
            kind = MID_SESSION

        start_ist = run[0].astimezone(IST)
        end_ist = (run[-1] + timedelta(minutes=timeframe_minutes)).astimezone(IST)
        gaps.append(Gap(
            kind=kind,
            start_ist=start_ist.strftime("%Y-%m-%d %H:%M"),
            end_ist=end_ist.strftime("%Y-%m-%d %H:%M"),
            minutes=len(run) * timeframe_minutes,
            missing_buckets=len(run),
        ))
        run.clear()

    for bucket in expected:
        if bucket in observed:
            close_run()
        else:
            run.append(bucket)
    close_run()
    return gaps


def assess_session(
    session_date: date,
    polls_by_bucket: dict[datetime, int],
    timeframe_minutes: int = 5,
    poll_seconds: int = 60,
) -> SessionCoverage:
    """Coverage for one trading session.

    `polls_by_bucket` maps a bucket start (UTC) to how many polls landed in
    it — for a stored bar that is its `samples` count.
    """
    expected = expected_bucket_starts(session_date, timeframe_minutes)
    per_bucket = max(1, int((timeframe_minutes * 60) // max(1, poll_seconds)))
    expected_set = set(expected)

    # Only buckets that belong to this session count towards it. A stray
    # out-of-hours bar is a separate finding, not extra credit here.
    observed = {b: n for b, n in polls_by_bucket.items() if b in expected_set and n > 0}

    coverage = SessionCoverage(
        session_date=session_date.isoformat(),
        expected_buckets=len(expected),
        observed_buckets=len(observed),
        expected_polls=len(expected) * per_bucket,
        observed_polls=sum(min(n, per_bucket) for n in observed.values()),
        gaps=find_gaps(expected, set(observed), timeframe_minutes),
        under_sampled_buckets=sum(1 for n in observed.values() if n < per_bucket),
    )

    if observed:
        first, last = min(observed), max(observed)
        coverage.first_ist = first.astimezone(IST).strftime("%Y-%m-%d %H:%M")
        coverage.last_ist = (last + timedelta(minutes=timeframe_minutes)) \
            .astimezone(IST).strftime("%Y-%m-%d %H:%M")

    return coverage


def assessable_sessions(first_seen: date | None, last_seen: date | None) -> list[date]:
    """Trading days that can be judged, given what the archive contains.

    Bounded by the first and last snapshot on purpose. Before the first,
    there is no basis for claiming the collector should have been running —
    saying otherwise would be inferring it, which this module refuses to do.
    Weekends and exchange holidays are excluded, and a year with no holiday
    list is excluded rather than guessed at.
    """
    if first_seen is None or last_seen is None:
        return []

    out: list[date] = []
    day = first_seen
    while day <= last_seen:
        if is_session(day) is True:
            out.append(day)
        day += timedelta(days=1)
    return out
