"""Angel One historical candles, paged backwards and checked on arrival.

Yahoo serves roughly sixty days of 5-minute data. Angel serves at least
five years of it, which is the difference between a sample that can settle
a question about the strategy and one that cannot: 73 sessions produce ~67
trades, and distinguishing this strategy's edge from noise needs several
hundred.

Three behaviours of `getCandleData` shape everything here, and all three
were measured rather than read in a document:

  1. **A request longer than 100 days is silently truncated.** It does not
     error. It returns the most *recent* 100 days and answers SUCCESS, so a
     naive 2-year request looks like a success and quietly delivers an
     eighth of the data. The pager therefore walks backwards in windows of
     at most `angel_history_max_window_days`, and every response is checked
     against the window that was asked for.

  2. **An unknown token returns success with an empty list.** Tokens
     99999999, 1 and 44444444 all answered `status=True, message=SUCCESS,
     data=[]` — indistinguishable from a real quiet window. So a token is
     validated against the instrument master *before* any fetch, and the
     two kinds of emptiness are different types in the result: an empty
     window for a validated token is `no_trades`, an empty window for an
     unvalidated one is an error.

  3. **Rate limiting is not an HTTP 429.** It arrives as the SDK failing to
     parse a non-JSON body — `DataException("Couldn't parse the JSON
     response received from the server")`. Retry logic keyed on status
     codes never sees it, so the backoff here catches that exception *by
     type*.

MERGE POLICY
------------
This importer writes only where the existing archive is silent.

`uq_candle` is UNIQUE on (symbol, timeframe, timestamp) — `source` is not
part of the key — and `import_index_candles` upserts with ON CONFLICT DO
UPDATE. Pointing a 2-year backfill at a range Yahoo already covers would
therefore overwrite every one of those bars and bump its revision: the
opposite of preserving them. Keeping both rows would need `source` in the
unique key, and the moment two rows shared a timestamp
`HistoricalFeed.__init__` would raise on the duplicate and every backtest
would stop.

So the policy is avoidance rather than resolution: `plan_backfill` clips
the requested range to the era the archive does not already hold, and
refuses to write into an occupied range. The overlap is still worth
knowing about — `compare_overlap` fetches a range Yahoo already covers and
reports how far the two vendors disagree, **in memory, storing nothing**.
That turns the merge question from a schema commitment into a measurement,
which is the only honest way to decide it later.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from ..config import get_settings

log = logging.getLogger(__name__)

# Angel's own field order in each candle row.
CANDLE_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")

# The source tag these rows carry. Deliberately not "angel_historical",
# which is exactly 16 characters against a varchar(16) column and would
# leave no room for a successor tag ever again.
SOURCE = "angel_hist"

# What the API calls a five-minute bar.
INTERVAL_5M = "FIVE_MINUTE"

# Where the public instrument dump lives. Live contracts only — expired
# ones are absent, which is why this module backfills the index and not
# option chains.
MASTER_URL = ("https://margincalculator.angelone.in/OpenAPI_File/files/"
              "OpenAPIScripMaster.json")

IST = "Asia/Kolkata"


class AngelHistoryError(RuntimeError):
    """The fetch could not be completed and the caller must not proceed."""


class UnvalidatedToken(AngelHistoryError):
    """A token that is not in the instrument master.

    Its own class because the failure it prevents is silent. Angel answers
    an unknown token with SUCCESS and no rows, so without this check a typo
    imports as "the market was closed for two years".
    """


@dataclass
class WindowReport:
    """One request: what was asked for, and what came back."""
    requested_from: datetime
    requested_to: datetime
    candles: int = 0
    first: datetime | None = None
    last: datetime | None = None
    retries: int = 0
    truncated: bool = False
    no_trades: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "from": self.requested_from.isoformat(),
            "to": self.requested_to.isoformat(),
            "candles": self.candles,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
            "retries": self.retries,
            "truncated": self.truncated,
            "no_trades": self.no_trades,
            "note": self.note,
        }


@dataclass
class FetchResult:
    """Every bar fetched, and every reason to distrust the set."""
    token: str
    interval: str
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    windows: list[WindowReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def candles(self) -> int:
        return len(self.frame)

    @property
    def truncated_windows(self) -> int:
        return sum(1 for w in self.windows if w.truncated)

    @property
    def empty_windows(self) -> int:
        return sum(1 for w in self.windows if w.no_trades)

    @property
    def clean(self) -> bool:
        """Whether every window returned what it was asked for."""
        return not self.warnings and self.truncated_windows == 0

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "interval": self.interval,
            "candles": self.candles,
            "requests": len(self.windows),
            "truncated_windows": self.truncated_windows,
            "empty_windows": self.empty_windows,
            "retries": sum(w.retries for w in self.windows),
            "clean": self.clean,
            "warnings": self.warnings,
        }


# ---- the instrument master -------------------------------------------

# Where a downloaded master is kept between runs. Not in `logs/` — this is
# state the desk reads back, not a record of what happened.
MASTER_CACHE_DIR = Path("var/cache")

# The file is ~34MB over one connection and the tail is the part that goes
# missing: observed truncating at 23.1MB and again at 8.4MB on 15-Sep-2026,
# both reported as a clean 200 whose body then stopped.
MASTER_ATTEMPTS = 3
MASTER_RETRY_SECONDS = 2.0


def _master_cache_file(directory: Path, day: date) -> Path:
    return directory / f"instrument-master-{day.isoformat()}.json"


def _cached_masters(directory: Path) -> list[tuple[date, Path]]:
    """Every cached master on disk, newest day first."""
    found: list[tuple[date, Path]] = []
    try:
        entries = list(directory.glob("instrument-master-*.json"))
    except OSError:
        return []
    for path in entries:
        stamp = path.stem.removeprefix("instrument-master-")
        try:
            found.append((date.fromisoformat(stamp), path))
        except ValueError:
            continue
    return sorted(found, reverse=True)


def _download_master(attempts: int, pause: float) -> str:
    """The master as text, retried, because the download is not reliable."""
    import httpx

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.get(MASTER_URL, timeout=120)
            response.raise_for_status()
            return response.text
        except Exception as exc:                          # noqa: BLE001
            last = exc
            log.warning("instrument master download attempt %d/%d failed: %s",
                        attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(pause * attempt)
    raise AngelHistoryError(
        f"could not download the instrument master after {attempts} "
        f"attempts: {last}")


def load_master(fetch=None, *, cache_dir: Path | str | None = None,
                on: date | None = None, attempts: int = MASTER_ATTEMPTS,
                pause: float = MASTER_RETRY_SECONDS) -> list[dict]:
    """The public instrument dump. ~140k rows, cached for the day.

    Three things happen here that did not before, and each of them was a
    live outage on 15-Sep-2026:

    **It is cached by date.** The master is republished once a morning, so
    fetching it again inside the same day is 34MB spent to receive what we
    already had. Every restart paid that toll, and the option stream sat
    dark for the ten seconds it took — longer when it failed.

    **The download is retried.** A single truncated body used to leave the
    desk with no option universe at all. The live option chain then falls
    back to the polled NSE snapshot, which is a minute behind instead of
    four hundred milliseconds, and nothing says so louder than one WARNING.

    **A stale copy beats no copy.** If today's master will not download but
    yesterday's is on disk, the desk uses yesterday's and says so. Expiries
    already listed stay listed and their tokens do not move; the only thing
    an old master can lack is a contract listed this morning. A chain built
    from yesterday's tokens is worth vastly more than no live chain, and
    `option_universe.build` still drops anything already expired.

    `fetch` stays the test seam it always was, and bypasses all of this.
    """
    if fetch is not None:
        return fetch()

    day = on or date.today()
    directory = Path(cache_dir) if cache_dir is not None else MASTER_CACHE_DIR
    todays = _master_cache_file(directory, day)

    if todays.exists():
        try:
            return json.loads(todays.read_text())
        except (OSError, ValueError) as exc:
            # A half-written or corrupt cache must not be sticky.
            log.warning("cached instrument master %s unreadable (%s); "
                        "fetching a fresh one", todays, exc)
            todays.unlink(missing_ok=True)

    try:
        text = _download_master(attempts, pause)
        rows = json.loads(text)
    except Exception as exc:                              # noqa: BLE001
        for stamp, path in _cached_masters(directory):
            if stamp >= day:
                continue
            try:
                rows = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            log.warning(
                "could not fetch today's instrument master (%s) — falling "
                "back to the copy from %s. Contracts listed since then are "
                "missing; everything already listed is unchanged.",
                exc, stamp)
            return rows
        raise

    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Written beside the target and moved into place, so a download cut
        # short cannot leave a half-file that reads as a valid cache.
        partial = todays.with_suffix(".partial")
        partial.write_text(text)
        partial.replace(todays)
    except OSError as exc:
        log.warning("could not cache the instrument master (%s); "
                    "it will be downloaded again next time", exc)
    else:
        for stamp, path in _cached_masters(directory):
            if stamp < day:
                path.unlink(missing_ok=True)

    return rows


def validate_token(token: str, master: list[dict]) -> dict:
    """Find `token` in the master, or refuse to go near the API.

    This is the guard for behaviour (2) above. It runs before any fetch,
    because afterwards there is no evidence left to distinguish a wrong
    token from a quiet market.
    """
    wanted = str(token)
    for row in master:
        if str(row.get("token")) == wanted:
            return row
    raise UnvalidatedToken(
        f"token {token} is not in Angel's instrument master. Refusing to "
        "fetch: an unknown token answers SUCCESS with no rows, so the "
        "result would be indistinguishable from a market that never traded."
    )


# ---- the pager --------------------------------------------------------

def _as_dt(value: date | datetime, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    t = datetime.min.time().replace(hour=15, minute=30) if end_of_day \
        else datetime.min.time().replace(hour=9, minute=15)
    return datetime.combine(value, t)


def plan_windows(start: datetime, end: datetime,
                 max_days: int | None = None) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into backwards windows of at most `max_days`.

    Backwards because a truncated response keeps the recent end: walking
    forwards, an over-long window would silently drop its *oldest* rows,
    which is precisely the era being backfilled.
    """
    max_days = max_days or get_settings().angel_history_max_window_days
    if max_days <= 0:
        raise ValueError("window must be at least one day")
    if end < start:
        raise ValueError(f"end {end} is before start {start}")

    windows: list[tuple[datetime, datetime]] = []
    cursor = end
    while cursor >= start:
        window_start = max(start, cursor - timedelta(days=max_days - 1))
        windows.append((window_start, cursor))
        if window_start <= start:
            break
        cursor = window_start - timedelta(days=1)
    return windows


def _call(client, params: dict, *, pace: float, max_retries: int,
          sleep=time.sleep) -> dict:
    """One `getCandleData`, paced, with backoff on the throttle.

    The throttle is caught by exception type rather than by message,
    because the message is the SDK's and not a contract.
    """
    from SmartApi.smartExceptions import DataException

    attempt = 0
    while True:
        try:
            response = client.getCandleData(params)
        except DataException as exc:
            attempt += 1
            if attempt > max_retries:
                raise AngelHistoryError(
                    f"Angel throttled {max_retries} retries in a row for "
                    f"{params.get('fromdate')}..{params.get('todate')}: {exc}"
                ) from exc
            backoff = pace * (2 ** attempt)
            log.warning("Angel throttled (attempt %s/%s), backing off %.1fs",
                        attempt, max_retries, backoff)
            sleep(backoff)
            continue

        sleep(pace)
        if not isinstance(response, dict):
            raise AngelHistoryError(
                f"Angel returned {type(response).__name__}, not a response")
        if response.get("status") is False:
            raise AngelHistoryError(
                f"Angel refused the candle request: "
                f"{response.get('message')} ({response.get('errorcode')})")
        return {"response": response, "retries": attempt}


def _to_frame(rows: list) -> pd.DataFrame:
    """Angel's list-of-lists into the platform's six-column shape."""
    if not rows:
        return pd.DataFrame(columns=list(CANDLE_FIELDS))
    frame = pd.DataFrame(rows, columns=list(CANDLE_FIELDS))
    # Angel stamps IST with an offset; normalise to UTC like every other
    # source in the archive.
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)


def fetch_index_candles(
    client,
    symbol_token: str,
    from_date: date | datetime,
    to_date: date | datetime,
    *,
    exchange: str = "NSE",
    interval: str = INTERVAL_5M,
    master: list[dict] | None = None,
    settings=None,
    sleep=time.sleep,
) -> FetchResult:
    """Every 5-minute bar between two dates, paged backwards and verified.

    `master` is required in spirit: pass the instrument dump so the token
    can be validated. Passing None fetches it, which is a network call —
    fine for a command, wrong inside a loop.
    """
    settings = settings or get_settings()
    start, end = _as_dt(from_date), _as_dt(to_date, end_of_day=True)

    validate_token(symbol_token, master if master is not None else load_master())

    result = FetchResult(token=str(symbol_token), interval=interval)
    frames: list[pd.DataFrame] = []

    for window_start, window_end in plan_windows(
            start, end, settings.angel_history_max_window_days):
        report = WindowReport(requested_from=window_start,
                              requested_to=window_end)
        params = {
            "exchange": exchange,
            "symboltoken": str(symbol_token),
            "interval": interval,
            "fromdate": window_start.strftime("%Y-%m-%d %H:%M"),
            "todate": window_end.strftime("%Y-%m-%d %H:%M"),
        }
        call = _call(client, params,
                     pace=settings.angel_history_pace_seconds,
                     max_retries=settings.angel_history_max_retries,
                     sleep=sleep)
        report.retries = call["retries"]
        rows = call["response"].get("data") or []
        frame = _to_frame(rows)

        report.candles = len(frame)
        if frame.empty:
            # The token was validated, so this is a real quiet window —
            # a market holiday stretch, or a contract not yet listed.
            report.no_trades = True
            report.note = "no bars in window (token is validated, so this is real)"
        else:
            report.first = frame["timestamp"].min().to_pydatetime()
            report.last = frame["timestamp"].max().to_pydatetime()
            _check_window(report, window_start, window_end, result)
            frames.append(frame)

        result.windows.append(report)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        before = len(combined)
        combined = (combined
                    .drop_duplicates(subset="timestamp", keep="last")
                    .sort_values("timestamp")
                    .reset_index(drop=True))
        if before != len(combined):
            # Adjacent windows share no dates, so this should never fire.
            # If it does, the pager's arithmetic is wrong and the caller
            # needs to know before the rows reach the database.
            result.warnings.append(
                f"{before - len(combined)} duplicate timestamps across "
                "windows — the pager overlapped, which it should not")
        result.frame = combined
    else:
        result.frame = pd.DataFrame(columns=list(CANDLE_FIELDS))

    log.info("Angel history %s: %s", symbol_token, result.to_dict())
    return result


def _check_window(report: WindowReport, window_start: datetime,
                  window_end: datetime, result: FetchResult) -> None:
    """Did the response actually cover the window we asked for?

    Behaviour (1): an over-long request keeps the recent end and answers
    SUCCESS. The pager should never send one, so a short response here is
    either a bug in the window arithmetic or the vendor changing its
    limit — both of which must be loud.
    """
    first = pd.Timestamp(report.first).tz_convert(IST)
    last = pd.Timestamp(report.last).tz_convert(IST)
    asked_from = pd.Timestamp(window_start).tz_localize(IST)
    asked_to = pd.Timestamp(window_end).tz_localize(IST)

    # A full trading day of slack at the start: the window may open on a
    # weekend or a holiday, and the first real bar legitimately arrives
    # later than the date requested.
    missing_head = (first.normalize() - asked_from.normalize()).days
    missing_tail = (asked_to.normalize() - last.normalize()).days

    if missing_head > 4 or missing_tail > 4:
        report.truncated = True
        report.note = (
            f"asked {asked_from:%Y-%m-%d}..{asked_to:%Y-%m-%d}, "
            f"got {first:%Y-%m-%d}..{last:%Y-%m-%d} "
            f"(head short {missing_head}d, tail short {missing_tail}d)")
        result.warnings.append(report.note)
        log.warning("Angel window did not cover the request: %s", report.note)


# ---- the overlap measurement -----------------------------------------

@dataclass
class OverlapReport:
    """How far Angel and the existing archive disagree. Nothing is stored."""
    bars_compared: int = 0
    only_in_angel: int = 0
    only_in_archive: int = 0
    close_matches: int = 0
    max_close_diff: float = 0.0
    mean_abs_close_diff: float = 0.0
    worst: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bars_compared": self.bars_compared,
            "only_in_angel": self.only_in_angel,
            "only_in_archive": self.only_in_archive,
            "close_matches": self.close_matches,
            "max_close_diff": round(self.max_close_diff, 4),
            "mean_abs_close_diff": round(self.mean_abs_close_diff, 4),
            "worst": self.worst,
        }


def compare_overlap(angel: pd.DataFrame, archive: pd.DataFrame,
                    *, tolerance: float = 0.05) -> OverlapReport:
    """Compare two vendors bar for bar. Read-only, by construction.

    This exists so the merge policy can be decided from evidence instead of
    taste. It never writes: the whole point of clipping the backfill to the
    unoccupied era is that no row is ever at risk, and a comparison that
    could store something would put that back.
    """
    report = OverlapReport()
    if angel.empty or archive.empty:
        report.only_in_angel = len(angel)
        report.only_in_archive = len(archive)
        return report

    a = angel.set_index("timestamp")
    b = archive.set_index("timestamp")
    shared = a.index.intersection(b.index)

    report.bars_compared = len(shared)
    report.only_in_angel = len(a.index.difference(b.index))
    report.only_in_archive = len(b.index.difference(a.index))
    if not len(shared):
        return report

    diff = (a.loc[shared, "close"] - b.loc[shared, "close"]).abs()
    report.close_matches = int((diff <= tolerance).sum())
    report.max_close_diff = float(diff.max())
    report.mean_abs_close_diff = float(diff.mean())
    report.worst = [
        {"timestamp": ts.isoformat(),
         "angel": float(a.loc[ts, "close"]),
         "archive": float(b.loc[ts, "close"]),
         "diff": round(float(diff.loc[ts]), 4)}
        for ts in diff.sort_values(ascending=False).head(5).index
    ]
    return report


# ---- the write plan ---------------------------------------------------

@dataclass
class BackfillPlan:
    """Which range will actually be written, and why it was clipped."""
    requested_start: date
    requested_end: date
    write_start: date | None
    write_end: date | None
    archive_start: date | None
    archive_end: date | None
    clipped_days: int = 0
    reason: str | None = None

    @property
    def writable(self) -> bool:
        return self.write_start is not None and self.write_end is not None

    def to_dict(self) -> dict:
        return {
            "requested": [self.requested_start.isoformat(),
                          self.requested_end.isoformat()],
            "writes": ([self.write_start.isoformat(), self.write_end.isoformat()]
                       if self.writable else None),
            "archive_holds": ([self.archive_start.isoformat(),
                               self.archive_end.isoformat()]
                              if self.archive_start else None),
            "clipped_days": self.clipped_days,
            "reason": self.reason,
        }


def plan_backfill(requested_start: date, requested_end: date,
                  archive_start: date | None,
                  archive_end: date | None) -> BackfillPlan:
    """Clip the request to the era the archive does not already hold.

    The merge policy, in one function. `uq_candle` cannot hold two sources
    for one bar, so rather than resolve a collision this refuses to create
    one — the requested range is trimmed to end the day before the archive
    begins.

    Only the leading era is written. A request that extends *past* the
    archive's end is clipped there too rather than split into two runs: a
    backfill that writes on both sides of live data is the shape most
    likely to be run twice by mistake.
    """
    plan = BackfillPlan(
        requested_start=requested_start, requested_end=requested_end,
        write_start=requested_start, write_end=requested_end,
        archive_start=archive_start, archive_end=archive_end)

    if archive_start is None or archive_end is None:
        plan.reason = "archive is empty; writing the whole requested range"
        return plan

    if requested_start >= archive_start:
        plan.write_start = plan.write_end = None
        plan.reason = (
            f"the archive already covers {archive_start}..{archive_end} and "
            f"the request starts at {requested_start}. Nothing would be "
            "written that did not overwrite an existing bar; refusing.")
        return plan

    if requested_end >= archive_start:
        plan.write_end = archive_start - timedelta(days=1)
        plan.clipped_days = (requested_end - plan.write_end).days
        plan.reason = (
            f"clipped to end {plan.write_end} — the archive already holds "
            f"{archive_start}..{archive_end}, and uq_candle cannot carry two "
            "sources for one bar")
    else:
        plan.reason = "requested range ends before the archive begins"
    return plan


def utc_now() -> datetime:
    return datetime.now(UTC)


# ---- the run ----------------------------------------------------------

@dataclass
class BackfillReport:
    """Everything a run did, in the order you would want to read it."""
    plan: BackfillPlan
    fetch: FetchResult | None = None
    stored: dict = field(default_factory=dict)
    per_day: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    overlap: OverlapReport | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "plan": self.plan.to_dict(),
            "fetch": self.fetch.to_dict() if self.fetch else None,
            "stored": self.stored,
            "sessions": len(self.per_day),
            "gaps": self.gaps,
            "overlap": self.overlap.to_dict() if self.overlap else None,
        }


# A full NIFTY session is 09:15..15:30 inclusive at five minutes: 76 bars.
FULL_SESSION_BARS = 76


def _session_date(moment: datetime | None) -> date | None:
    """The IST trading day a stored instant belongs to.

    SQLite has no timezone type, so the same column comes back tz-aware
    from Postgres and tz-naive from SQLite. Everything in the archive is
    written as UTC, so a naive value is localised rather than guessed at —
    reading it as local time would move the seam by five and a half hours
    and clip the backfill a day short.
    """
    if moment is None:
        return None
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(UTC)
    return stamp.tz_convert(IST).date()


def session_counts(frame: pd.DataFrame) -> list[dict]:
    """Bars per trading day, so an anomalous session is visible at a glance."""
    if frame.empty:
        return []
    ist = frame["timestamp"].dt.tz_convert(IST)
    counts = ist.dt.date.value_counts().sort_index()
    return [{"day": str(day), "bars": int(n),
             "short_by": max(0, FULL_SESSION_BARS - int(n))}
            for day, n in counts.items()]


def find_gaps(per_day: list[dict], *, min_short: int = 5) -> list[dict]:
    """Sessions materially short of a full day.

    Not every short day is wrong — expiry days and exchange half-days are
    real — so this reports rather than rejects. `min_short` keeps the
    single missing bar at the close from filling the report with noise.
    """
    return [d for d in per_day if d["short_by"] >= min_short]


def run_backfill(
    db,
    client,
    *,
    start: date,
    end: date,
    symbol: str | None = None,
    timeframe: str = "5m",
    token: str | None = None,
    master: list[dict] | None = None,
    settings=None,
    dry_run: bool = False,
    sleep=time.sleep,
) -> BackfillReport:
    """Fetch, clip, validate and store — in that order.

    Clipping happens before fetching so a run that would only overwrite
    existing bars costs nothing and reaches no network. Storage goes
    through `import_index_candles`, so these rows meet exactly the same
    validation gate and carry the same provenance as any live candle; the
    only thing distinguishing them is `source`.
    """
    from . import importer, repository

    settings = settings or get_settings()
    symbol = symbol or settings.watch_symbol
    token = token or settings.angel_history_index_token

    coverage = repository.coverage(db, symbol, timeframe)
    # `Coverage.first/last` are UTC instants. The plan reasons in trading
    # days, and 09:15 IST is the previous UTC date — comparing the two
    # without converting would clip the backfill a day short at the seam.
    plan = plan_backfill(start, end,
                         _session_date(coverage.first), _session_date(coverage.last))
    report = BackfillReport(plan=plan, dry_run=dry_run)

    if not plan.writable:
        log.warning("Angel backfill refused: %s", plan.reason)
        return report

    result = fetch_index_candles(
        client, token, plan.write_start, plan.write_end,
        master=master, settings=settings, sleep=sleep)
    report.fetch = result
    report.per_day = session_counts(result.frame)
    report.gaps = find_gaps(report.per_day)

    if dry_run:
        log.info("Angel backfill dry run: %s bars, nothing written",
                 result.candles)
        return report

    if result.frame.empty:
        report.stored = {"skipped": "no bars fetched"}
        return report

    import_report = importer.import_index_candles(
        db, result.frame, symbol, timeframe, SOURCE)
    db.commit()
    report.stored = import_report.to_dict()
    return report


# ---- filling holes inside the archive --------------------------------
#
# `run_backfill` only extends the archive backwards, so a session the live
# collector missed (the machine was off, the feed was down) stays missing
# forever. Filling one is safe under the same merge policy for a narrower
# reason: only timestamps the archive does not hold are written, so the
# upsert can only ever insert. An existing bar is never restated.

@dataclass
class GapFillReport:
    """Which sessions were short, what was fetched, and what was added."""
    requested_start: date
    requested_end: date
    missing: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    fetch: FetchResult | None = None
    new_bars: int = 0
    per_day_added: dict[str, int] = field(default_factory=dict)
    overlap: OverlapReport | None = None
    stored: dict = field(default_factory=dict)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "requested": [self.requested_start.isoformat(),
                          self.requested_end.isoformat()],
            "missing_sessions": self.missing,
            "skipped": self.skipped,
            "fetch": self.fetch.to_dict() if self.fetch else None,
            "new_bars": self.new_bars,
            "added_per_day": self.per_day_added,
            "overlap_with_existing": self.overlap.to_dict() if self.overlap else None,
            "stored": self.stored,
        }


def find_missing_sessions(
    db, *, symbol: str, timeframe: str, start: date, end: date,
    today: date, min_short: int = 5,
) -> tuple[list[dict], list[dict]]:
    """Trading days in [start, end] the archive holds materially short.

    Returns (missing, skipped). Today and later are skipped because the
    live collector is still writing them; a year with no holiday list is
    skipped because a weekday there may not have been a session, and
    fetching it would only report a gap that is not one.
    """
    from .. import market_calendar
    from . import repository

    sessions, unverified = market_calendar.sessions_between(start, end)
    skipped = [{"day": d.isoformat(), "reason": "no verified holiday list for the year"}
               for d in unverified]
    past = [d for d in sessions if d < today]
    skipped += [{"day": d.isoformat(), "reason": "today or later; the live feed owns it"}
                for d in sessions if d >= today]
    if not past:
        return [], skipped

    stored = repository.load_index_candles(
        db, symbol=symbol, timeframe=timeframe, start=past[0], end=past[-1])
    counts: dict[date, int] = {}
    if not stored.empty:
        days = stored["timestamp"].dt.tz_convert(IST).dt.date
        counts = days.value_counts().to_dict()

    missing = []
    for day in past:
        bars = int(counts.get(day, 0))
        if FULL_SESSION_BARS - bars >= min_short:
            missing.append({"day": day.isoformat(), "stored_bars": bars})
    return missing, skipped


def fill_gaps(
    db,
    client,
    *,
    start: date,
    end: date,
    symbol: str | None = None,
    timeframe: str = "5m",
    token: str | None = None,
    master: list[dict] | None = None,
    settings=None,
    dry_run: bool = False,
    today: date | None = None,
    sleep=time.sleep,
) -> GapFillReport:
    """Fetch the short sessions in [start, end] and insert only absent bars.

    A session that is partly stored keeps every bar it has; Angel supplies
    the rest. The vendors are measured against each other on the bars both
    hold, so a seam inside a mixed day is reported rather than hidden.
    """
    from . import importer, repository

    settings = settings or get_settings()
    symbol = symbol or settings.watch_symbol
    token = token or settings.angel_history_index_token
    today = today or pd.Timestamp(utc_now()).tz_convert(IST).date()

    report = GapFillReport(requested_start=start, requested_end=end, dry_run=dry_run)
    report.missing, report.skipped = find_missing_sessions(
        db, symbol=symbol, timeframe=timeframe, start=start, end=end, today=today)
    if not report.missing:
        return report

    days = [date.fromisoformat(m["day"]) for m in report.missing]
    result = fetch_index_candles(client, token, days[0], days[-1],
                                 master=master, settings=settings, sleep=sleep)
    report.fetch = result
    if result.frame.empty:
        report.stored = {"skipped": "Angel returned no bars for those sessions"}
        return report

    frame = result.frame
    on_missing_day = frame["timestamp"].dt.tz_convert(IST).dt.date.isin(set(days))
    frame = frame[on_missing_day]

    existing = repository.load_index_candles(
        db, symbol=symbol, timeframe=timeframe, start=days[0], end=days[-1])
    held = set(existing["timestamp"]) if not existing.empty else set()
    report.overlap = compare_overlap(frame, existing)

    fresh = frame[~frame["timestamp"].isin(held)].reset_index(drop=True)
    report.new_bars = len(fresh)
    if not fresh.empty:
        added = fresh["timestamp"].dt.tz_convert(IST).dt.date.value_counts()
        report.per_day_added = {str(d): int(n) for d, n in sorted(added.items())}

    if dry_run:
        return report
    if fresh.empty:
        report.stored = {"skipped": "every fetched bar is already stored"}
        return report

    import_report = importer.import_index_candles(
        db, fresh, symbol, timeframe, SOURCE)
    if import_report.write.updated:
        # Unreachable while `fresh` excludes held timestamps. If it ever
        # fires, an existing bar was restated and the run must not commit.
        db.rollback()
        raise AngelHistoryError(
            f"gap fill would have restated {import_report.write.updated} "
            "existing bars; rolled back")
    db.commit()
    report.stored = import_report.to_dict()
    return report
