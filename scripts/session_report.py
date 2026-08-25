"""End-of-session check: did the desk record what it was supposed to?

Run after the close. It answers the three questions that matter once a
session is over and cannot be re-run:

  Did every dataset capture the session?  Index candles backfill from Yahoo;
  option snapshots do not backfill at all, so a gap there is permanent and
  is the only finding here graded as loss rather than as a caveat.

  Did the analysis layers record alongside the signals?  A signal without a
  regime, a bias and an entry state is a row that cannot be studied later,
  and the whole point of Phase 3 onward is that every decision is
  reconstructable.

  Is the collector healthy *now*, as distinct from healthy on average?  A
  38% coverage figure across seven sessions says nothing about whether
  today worked. This reports per-session so a single dead day is visible
  instead of being averaged into a number that looks merely disappointing.

Written because a session was lost in exactly the way this catches. On
2026-08-24 the container's DNS resolution failed intermittently for a day.
The index archive filled normally from Yahoo, the option collector logged
seventy-five warm-up failures and three snapshot errors, and stored nothing
for the entire session. Nothing announced it. The data-quality report
averaged the hole into a coverage percentage, and the loss was found the
next morning by someone looking for something else.

Read-only. It changes nothing and decides nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Same resolution `doctor.py` uses: the repo checkout puts the package under
# ./backend, the container image puts it at /srv, and this script is meant to
# run from either.
ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "backend", Path("/srv")):
    if (candidate / "app").is_dir():
        sys.path.insert(0, str(candidate))
        break

from app.config import get_settings
from app.data import quality, regime_store, repository
from app.db import SessionLocal
from app.market_hours import IST, MARKET_CLOSE, MARKET_OPEN, is_trading_date, to_ist
from app.models import CandleRecord, MarketRegime, OptionCandle, OptionContract, SignalRecord
from sqlalchemy import func, select

# 09:15 to 15:25 inclusive, five minutes apart.
EXPECTED_INDEX_BARS = 75

# One poll a minute folded into five-minute bars.
EXPECTED_SAMPLES_PER_BAR = 5

# Below this share of the expected option samples, the session is treated as
# a loss rather than a shortfall. Deliberately generous: a few missed polls
# is a slow source, half a session missing is a broken collector.
OPTION_LOSS_THRESHOLD = 0.50

# Where the running desk answers. The report is normally run inside the
# backend container, so localhost is the app itself.
DEFAULT_API = "http://localhost:8000"

# A health check that hangs is worse than one that fails: this runs after the
# close, unattended, and must always print something.
SCHEDULER_TIMEOUT_SECONDS = 5

# Below this share of the runs a job should have completed in the window it
# was actually observed for, the scheduler was not keeping up. Not a tight
# bound on purpose — a handful of missed polls is a slow source, and only a
# systematic shortfall is a scheduler problem.
SCHEDULER_COMPLETION_FLOOR = 0.90

# The scheduled jobs, by the id each `start()` registers, and the setting
# that decides how often each one fires.
JOB_INTERVALS = {
    "nifty-agent": lambda s: s.agent_interval_minutes * 60,
    "option-collector": lambda s: s.option_snapshot_interval_seconds,
    "price-ticker": lambda s: s.ticker_interval_seconds,
}


def session_of(argument: str | None) -> date:
    if argument:
        return date.fromisoformat(argument)
    now = to_ist(datetime.now(IST))
    # Before the open, "the last session" is the previous trading day.
    day = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    for _ in range(10):
        if is_trading_date(day):
            return day
        day -= timedelta(days=1)
    return day


def fetch_scheduler(base_url: str) -> tuple[dict | None, str | None]:
    """Read `/health/scheduler` off the running desk. Never raises.

    Returns `(payload, reason_it_is_missing)` — exactly one of which is set.
    A report that cannot reach the app must still print, and must say the
    scheduler is *unknown* rather than quietly implying it was fine.
    """
    try:
        import httpx
    except ImportError:
        return None, "httpx is not installed in this environment"

    url = f"{base_url.rstrip('/')}/health/scheduler"
    try:
        response = httpx.get(url, timeout=SCHEDULER_TIMEOUT_SECONDS)
    except Exception as exc:
        # Connection refused is the ordinary case: the desk is not running.
        # That is a fact to report, not a fault to raise.
        return None, f"{type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    try:
        return response.json(), None
    except ValueError:
        return None, "the endpoint did not return JSON"


def scheduler_health(day: date, payload: dict | None, reason: str | None,
                     now: datetime | None = None) -> dict:
    """Turn the endpoint's process-lifetime counters into a session statement.

    The care in here is all about one thing: the watchdog counts since the
    process started, and this report is about a session. Those two windows
    are not the same, and pretending they are would produce the exact class
    of finding this whole script exists to avoid — a desk that restarted at
    14:00 reported as having missed the morning, or a session the counters
    never covered reported as clean.

    So the observation window is the *overlap* of the two, and everything
    downstream is measured against that. When the overlap is empty — the app
    is off, or was started after this session closed — the answer is
    "unknown", never a number.
    """
    now = now or datetime.now(IST)
    block: dict = {"available": payload is not None, "reason": reason,
                   "covers_session": False, "jobs": {}, "starvation": []}
    if payload is None:
        return block

    block["healthy"] = payload.get("healthy")
    block["market_open"] = payload.get("market_open")
    block["problems"] = payload.get("problems") or []

    counting_since = payload.get("counting_since")
    if not counting_since:
        # The endpoint answered but the scheduler was never attached in that
        # process. Counts of zero here mean "not running", not "ran and did
        # nothing", and the difference matters.
        block["note"] = ("The app is up but its scheduler has not started, so "
                         "there are no counts to attribute to this session.")
        return block

    started = to_ist(datetime.fromisoformat(counting_since))
    session_open = datetime.combine(day, MARKET_OPEN, tzinfo=IST)
    session_close = datetime.combine(day, MARKET_CLOSE, tzinfo=IST)

    window_start = max(session_open, started)
    window_end = min(session_close, now)
    observed = max(0.0, (window_end - window_start).total_seconds())
    session_seconds = (session_close - session_open).total_seconds()

    block.update({
        "counting_since": started.isoformat(),
        "observed_seconds": round(observed),
        "session_seconds": round(session_seconds),
        "observed_share": round(observed / session_seconds, 3) if session_seconds else 0.0,
        "covers_session": observed > 0,
    })

    if observed <= 0:
        block["note"] = (
            "This process began counting at "
            f"{started.strftime('%d-%b %H:%M')}, which does not overlap the "
            "session. Its counters describe a different window and say "
            "nothing about this one.")
        return block

    settings = get_settings()
    for job_id, interval_of in JOB_INTERVALS.items():
        health = (payload.get("jobs") or {}).get(job_id)
        interval = interval_of(settings)
        expected = int(observed // interval) if interval > 0 else 0
        if health is None:
            # The window overlaps, so this job should have been firing. A job
            # the scheduler has never reported is a job that never ran.
            block["jobs"][job_id] = {
                "expected": expected, "completed": 0, "skipped": 0,
                "errors": 0, "starved": None, "interval_seconds": interval,
                "note": "the scheduler has no record of this job running"}
            continue
        block["jobs"][job_id] = {
            "expected": expected,
            "completed": health.get("successes", 0),
            "skipped": health.get("skips", 0),
            "errors": health.get("errors", 0),
            "missed": health.get("missed", 0),
            "starved": health.get("starved"),
            "consecutive_skips": health.get("consecutive_skips", 0),
            "last_success": health.get("last_success"),
            "interval_seconds": interval,
        }
        if health.get("skips"):
            block["starvation"].append(
                f"{job_id}: {health['skips']} run(s) skipped because the "
                f"previous one was still going")
    return block


def check(db, day: date, symbol: str = "NIFTY", timeframe: str = "5m",
          scheduler: dict | None = None) -> dict:
    report: dict = {"session_date": day.isoformat(),
                    "trading_day": is_trading_date(day),
                    "problems": [], "losses": []}

    if not report["trading_day"]:
        report["note"] = "Not a trading day; nothing was expected."
        return report

    # --- index candles ---------------------------------------------------
    bars = db.scalar(select(func.count(CandleRecord.id)).where(
        CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe,
        CandleRecord.session_date == day)) or 0
    report["index"] = {"bars": bars, "expected": EXPECTED_INDEX_BARS,
                       "complete": bars >= EXPECTED_INDEX_BARS}
    if bars == 0:
        report["losses"].append("No index candles stored for this session.")
    elif bars < EXPECTED_INDEX_BARS:
        report["problems"].append(
            f"Index candles short: {bars} of {EXPECTED_INDEX_BARS}. Yahoo "
            "backfills, so this is recoverable with POST /data/import/index.")

    # --- option snapshots: the unrecoverable one -------------------------
    option_bars, samples, contracts = db.execute(
        select(func.count(func.distinct(OptionCandle.timestamp)),
               func.coalesce(func.sum(OptionCandle.samples), 0),
               func.count(func.distinct(OptionCandle.contract_id)))
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == symbol,
               OptionCandle.session_date == day)).one()

    per_contract = (samples / contracts) if contracts else 0
    expected = EXPECTED_INDEX_BARS * EXPECTED_SAMPLES_PER_BAR
    share = per_contract / expected if expected else 0
    report["options"] = {
        "bars": option_bars, "contracts": contracts,
        "samples_per_contract": round(per_contract, 1),
        "expected_samples": expected, "coverage": round(share, 3)}

    if option_bars == 0:
        report["losses"].append(
            "No option snapshots for the entire session. Option history "
            "cannot be backfilled — this session is gone. Check DNS from the "
            "container and the NSE endpoint before the next open.")
    elif share < OPTION_LOSS_THRESHOLD:
        report["losses"].append(
            f"Option coverage {share:.0%} of expected. The missing portion "
            "cannot be recovered.")

    # --- did the analysis layers record? ---------------------------------
    signals, with_bias, with_entry = db.execute(
        select(func.count(SignalRecord.id),
               func.count(SignalRecord.bias),
               func.count(SignalRecord.entry_state))
        .where(SignalRecord.symbol == symbol,
               SignalRecord.created_at >= datetime.combine(day, datetime.min.time(),
                                                           tzinfo=IST),
               SignalRecord.created_at < datetime.combine(day + timedelta(days=1),
                                                          datetime.min.time(),
                                                          tzinfo=IST))).one()
    regimes = db.scalar(select(func.count(MarketRegime.id)).where(
        MarketRegime.symbol == symbol, MarketRegime.timeframe == timeframe,
        MarketRegime.session_date == day)) or 0

    report["analysis"] = {"signals": signals, "with_bias": with_bias,
                          "with_entry_state": with_entry, "regime_bars": regimes}

    # Only sessions from the day the two-layer read went live can be missing
    # it. The cutoff is derived from the data rather than hard-coded: the
    # first signal that ever carried a bias. Without this, every session in
    # the archive that predates the feature reports a permanent problem, and
    # a report that always complains is a report nobody reads.
    first_planned = db.scalar(select(func.min(SignalRecord.created_at))
                              .where(SignalRecord.bias.isnot(None)))
    planned_from = to_ist(first_planned).date() if first_planned else None
    report["analysis"]["two_layer_live_from"] = (
        planned_from.isoformat() if planned_from else None)

    if signals and planned_from and day >= planned_from and with_bias < signals:
        report["problems"].append(
            f"{signals - with_bias} signal(s) stored without a bias — the "
            "two-layer read did not run for them.")
    elif signals and (planned_from is None or day < planned_from):
        report["analysis"]["note"] = (
            "Session predates the bias/entry-state columns; their absence is "
            "expected and is not counted as a problem.")
    # A trading session with candles but no signals means the agent was not
    # running, and that is the symptom nobody noticed on 2026-08-24: the
    # index archive looked fine because Yahoo backfills, so the only visible
    # trace of a starved scheduler was an empty signals table for the day.
    expected_ticks = EXPECTED_INDEX_BARS  # the agent ticks on the bar cadence
    if bars and signals == 0:
        report["problems"].append(
            "No signals at all for a session that has candles — the agent did "
            "not run. Signals cannot be recreated after the fact; a decision "
            "invented later is not a record of what the desk thought.")
    elif bars and signals < expected_ticks * 0.5:
        report["problems"].append(
            f"Only {signals} signal(s) for {bars} bars. The agent ticks on the "
            "bar cadence, so this is a scheduler that was starved or blocked — "
            "check for slow outbound calls holding a job past its interval.")

    if bars and regimes < bars:
        report["problems"].append(
            f"Regime rows ({regimes}) trail index bars ({bars}); the "
            "classifier is behind. Recoverable: POST /data/regimes/backfill.")

    # --- collector health, as of now -------------------------------------
    report["collector_now"] = [
        f.to_dict() for f in quality.option_collector_liveness(db, symbol)]

    # --- the platform's own quality verdict ------------------------------
    full = quality.report(db, symbol, timeframe)
    report["quality"] = {
        "verdict": full["verdict"], "errors": full["errors"],
        "index_backtest_eligible": full["index"]["backtest_eligible"],
        "options_backtest_eligible": full["options"]["backtest_eligible"]}

    # --- the scheduler, if the desk is up to be asked ---------------------
    #
    # Everything above is read from what was *stored*. This is the only part
    # read from the running process, and it answers the question the stored
    # data can only imply: were cycles being lost while the session ran.
    #
    # Graded only when the counters actually overlap this session. An
    # unreachable app is reported and never counted against the session — a
    # desk that is deliberately off must not read as a broken one, and the
    # loss lines above already catch a desk that was off when it should not
    # have been.
    if scheduler is not None:
        report["scheduler"] = scheduler
        if scheduler.get("covers_session"):
            for job_id, job in scheduler["jobs"].items():
                if job.get("starved"):
                    report["problems"].append(
                        f"Scheduler starved {job_id}: "
                        f"{job['consecutive_skips']} consecutive runs skipped. "
                        "Each skipped run is a cycle of data that was not "
                        "collected.")
                elif job["expected"] and job["completed"] < (
                        job["expected"] * SCHEDULER_COMPLETION_FLOOR):
                    report["problems"].append(
                        f"{job_id} completed {job['completed']} of about "
                        f"{job['expected']} runs due in the {scheduler['observed_share']:.0%} "
                        "of the session this process was up.")

    report["regime_coverage"] = regime_store.coverage(db, symbol, timeframe)
    report["archive"] = repository.coverage(db, symbol, timeframe).to_dict()
    report["ok"] = not report["losses"] and not report["problems"]
    return report


def render_scheduler(block: dict | None) -> list[str]:
    """The scheduler section, which says "unknown" more often than not.

    Deliberately verbose about *why* it cannot say anything, because the
    silent version of this — a missing section — is indistinguishable from a
    healthy one, and that is precisely the confusion that let two sessions
    drain away while the desk looked fine.
    """
    if block is None:
        return []
    if not block["available"]:
        return [f"  scheduler       unknown — {block['reason']}",
                "                  The desk was not reachable, so no cycle "
                "counts are claimed for this session."]
    if not block["covers_session"]:
        return [f"  scheduler       unknown — {block.get('note', 'no overlap with this session')}"]

    state = "healthy" if block.get("healthy") else "DEGRADED"
    lines = [f"  scheduler       {state} — counters cover "
             f"{block['observed_share']:.0%} of the session "
             f"(since {block['counting_since'][11:16]})"]
    for job_id, job in sorted(block["jobs"].items()):
        note = f"  {job['note']}" if job.get("note") else ""
        lines.append(
            f"    {job_id:<17} {job['completed']}/{job['expected']} done  "
            f"skipped={job['skipped']}  errors={job['errors']}{note}")
    for event in block["starvation"]:
        lines.append(f"  STARVED   {event}")
    return lines


def render(report: dict) -> str:
    lines = [f"QuantDesk session report — {report['session_date']}"]
    if not report["trading_day"]:
        return "\n".join(lines + ["  " + report["note"]])

    i, o, a = report["index"], report["options"], report["analysis"]
    lines += [
        f"  index candles   {i['bars']}/{i['expected']}"
        f"{'  OK' if i['complete'] else '  SHORT'}",
        f"  option samples  {o['samples_per_contract']}/{o['expected_samples']} "
        f"per contract ({o['coverage']:.0%}) over {o['contracts']} contracts",
        f"  regime bars     {a['regime_bars']}",
        f"  signals         {a['signals']}  bias={a['with_bias']}  "
        f"entry_state={a['with_entry_state']}",
        f"  quality verdict {report['quality']['verdict']}",
    ]
    for loss in report["losses"]:
        lines.append(f"  LOSS      {loss}")
    for problem in report["problems"]:
        lines.append(f"  PROBLEM   {problem}")
    for finding in report["collector_now"]:
        lines.append(f"  COLLECTOR {finding['severity']}: {finding['summary']}")
    lines += render_scheduler(report.get("scheduler"))
    if report["ok"]:
        lines.append("  Everything the session was supposed to record, it recorded.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="IST session date (YYYY-MM-DD)")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--api", default=DEFAULT_API,
                        help="base URL of the running desk, for scheduler health")
    parser.add_argument("--no-api", action="store_true",
                        help="skip the scheduler check entirely")
    args = parser.parse_args()

    day = session_of(args.date)
    scheduler = None
    if not args.no_api:
        payload, reason = fetch_scheduler(args.api)
        scheduler = scheduler_health(day, payload, reason)

    with SessionLocal() as db:
        report = check(db, day, args.symbol, args.timeframe, scheduler)

    print(json.dumps(report, default=str, indent=2) if args.json else render(report))
    # Non-zero only on unrecoverable loss. A short index day is recoverable
    # and should not page anyone.
    return 2 if report.get("losses") else 0


if __name__ == "__main__":
    raise SystemExit(main())
