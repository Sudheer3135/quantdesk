"""Backfill index candles from Angel One. Run by hand, never on a schedule.

Writes only where the archive is silent. If the requested range overlaps
existing candles the run is clipped to stop the day before they begin, and
if it overlaps entirely it is refused before a single request goes out —
`uq_candle` is unique on (symbol, timeframe, timestamp) and the importer
upserts, so writing into an occupied range would overwrite the existing
vendor's bars rather than sit beside them.

    # See what would happen. No writes, no surprises.
    python3 scripts/angel_backfill.py --start 2024-09-01 --dry-run

    # Do it.
    python3 scripts/angel_backfill.py --start 2024-09-01

    # Measure the two vendors against each other. Never writes.
    python3 scripts/angel_backfill.py --compare-overlap

    # A year of India VIX daily closes, for strategy v2's volatility gate.
    python3 scripts/angel_backfill.py --vix --start 2025-09-01 --dry-run

    # Fill sessions the live collector missed. Inserts only absent bars.
    python3 scripts/angel_backfill.py --fill-gaps --start 2026-09-01 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

for _candidate in (Path(__file__).resolve().parents[1] / "backend", Path("/srv")):
    if _candidate.exists():
        sys.path.insert(0, str(_candidate))

from app.brokers import angel
from app.config import get_settings
from app.data import angel_history as ah
from app.data import repository
from app.db import SessionLocal


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def show_plan(report: ah.BackfillReport) -> None:
    print(json.dumps(report.plan.to_dict(), indent=2))


def summarise(report: ah.BackfillReport) -> None:
    fetch = report.fetch
    print()
    print("--- fetch ---------------------------------------------------")
    print(json.dumps(fetch.to_dict() if fetch else {}, indent=2))

    if report.gaps:
        print()
        print(f"--- {len(report.gaps)} short sessions --------------------")
        for gap in report.gaps[:20]:
            print(f"  {gap['day']}  {gap['bars']:>3} bars "
                  f"(short by {gap['short_by']})")
        if len(report.gaps) > 20:
            print(f"  ... and {len(report.gaps) - 20} more")
    elif report.per_day:
        print(f"\nevery one of {len(report.per_day)} sessions is complete.")

    print()
    print("--- stored --------------------------------------------------")
    print(json.dumps(report.stored, indent=2, default=str))


def compare(args) -> int:
    """Fetch a range the archive already holds and diff it. Stores nothing."""
    settings = get_settings()
    with SessionLocal() as db:
        coverage = repository.coverage(db, settings.watch_symbol, "5m")
        if coverage.empty:
            print("the archive is empty; nothing to compare against")
            return 1
        start = ah._session_date(coverage.first)
        end = min(ah._session_date(coverage.last),
                  start + timedelta(days=args.compare_days))

        print(f"comparing {start} .. {end} — in memory, nothing is written")
        _, client = angel.login_with_client()
        result = ah.fetch_index_candles(
            client, settings.angel_history_index_token, start, end)
        archive = repository.load_index_candles(db, start=start, end=end)

    report = ah.compare_overlap(result.frame, archive)
    print(json.dumps(report.to_dict(), indent=2))
    if report.bars_compared:
        agree = 100 * report.close_matches / report.bars_compared
        print(f"\n{agree:.1f}% of shared bars agree on close to within 0.05")
    return 0


def vix(args, end: date) -> int:
    """India VIX daily closes into `vix_daily`. Inserts only absent sessions."""
    from app.strategy_v2 import vix as vix_history

    token = get_settings().angel_vix_token
    print(f"fetching India VIX (token {token}) daily {args.start} .. {end}"
          f"{' (dry run)' if args.dry_run else ''}")
    print("loading the instrument master (~140k rows) ...")
    master = ah.load_master()
    _, client = angel.login_with_client()
    print("logged in.\n")

    frame = vix_history.fetch_daily(client, token, args.start, end, master=master)
    print(f"{len(frame)} sessions fetched", end="")
    if not frame.empty:
        print(f", {frame['session_date'].min()} .. {frame['session_date'].max()}, "
              f"close {frame['close'].min():.2f} .. {frame['close'].max():.2f}")
    else:
        print()
    if args.dry_run or frame.empty:
        return 0
    with SessionLocal() as db:
        result = vix_history.store(db, frame, vix_history.SOURCE_HISTORY)
        print(json.dumps(result | {"coverage": vix_history.coverage(db)}, indent=2))
    return 0


def fill(args, end: date) -> int:
    """Insert the bars missing from short sessions. Never restates a bar."""
    print(f"filling short {args.symbol} {args.timeframe} sessions "
          f"{args.start} .. {end}{' (dry run)' if args.dry_run else ''}")
    with SessionLocal() as db:
        missing, skipped = ah.find_missing_sessions(
            db, symbol=args.symbol, timeframe=args.timeframe,
            start=args.start, end=end,
            today=ah._session_date(ah.utc_now()))
        if not missing:
            print("no short sessions in that range; nothing to fetch.")
            for s in skipped:
                print(f"  skipped {s['day']}: {s['reason']}")
            return 0
        for m in missing:
            print(f"  {m['day']}  holds {m['stored_bars']} bars")

        print("loading the instrument master (~140k rows) ...")
        master = ah.load_master()
        _, client = angel.login_with_client()
        print("logged in.\n")
        report = ah.fill_gaps(
            db, client, start=args.start, end=end, symbol=args.symbol,
            timeframe=args.timeframe, token=args.token, master=master,
            dry_run=args.dry_run)

    print(json.dumps(report.to_dict(), indent=2, default=str))
    verb = "would add" if args.dry_run else "added"
    print(f"\n{verb} {report.new_bars} bars: {report.per_day_added}")
    if report.fetch and not report.fetch.clean:
        print("\nWARNING: some windows did not cover their request.")
        return 2
    return 0


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_day,
                        help="first day to fetch (YYYY-MM-DD)")
    parser.add_argument("--end", type=parse_day, default=None,
                        help="last day; defaults to yesterday")
    parser.add_argument("--symbol", default=settings.watch_symbol)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--token", default=settings.angel_history_index_token)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, write nothing")
    parser.add_argument("--compare-overlap", action="store_true",
                        help="diff Angel against the existing archive")
    parser.add_argument("--compare-days", type=int, default=30)
    parser.add_argument("--fill-gaps", action="store_true",
                        help="fill short sessions inside the archive")
    parser.add_argument("--vix", action="store_true",
                        help="fetch India VIX daily closes for strategy v2")
    args = parser.parse_args()

    if args.compare_overlap:
        return compare(args)
    if args.start is None:
        parser.error("--start is required (or use --compare-overlap)")

    end = args.end or (date.today() - timedelta(days=1))
    if args.fill_gaps:
        return fill(args, end)
    if args.vix:
        return vix(args, end)

    print(f"backfilling {args.symbol} {args.timeframe} "
          f"{args.start} .. {end} from token {args.token}")
    print("loading the instrument master (~140k rows) ...")
    master = ah.load_master()

    _, client = angel.login_with_client()
    print("logged in.\n")

    with SessionLocal() as db:
        report = ah.run_backfill(
            db, client, start=args.start, end=end, symbol=args.symbol,
            timeframe=args.timeframe, token=args.token, master=master,
            dry_run=args.dry_run)

    show_plan(report)
    if not report.plan.writable:
        return 1
    summarise(report)

    if report.fetch and not report.fetch.clean:
        print("\nWARNING: some windows did not cover their request. "
              "The warnings above name the exact dates.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
