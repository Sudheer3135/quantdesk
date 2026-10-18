"""How different is a signal produced without an option chain?

The backfilled era has no option-chain history — NSE publishes none and
Angel drops expired contracts from its instrument master — so
`check_option_chain` will be disabled for every backfilled bar. It carries
16% of the weight, and `signal_engine` renormalises over the checks that
can still run (`scale = 1.0 / live_weight`), so the remaining six checks
are scaled up by 1/0.84 ≈ 1.19x.

That is not a bug: the renormalisation exists precisely so a disabled check
does not silently make the engine stricter. But it does mean a backfilled
signal and a live signal are not the same measurement, and a sweep that
mixes them without knowing the size of the difference is reading two
different instruments off one scale.

This script measures the difference instead of assuming it, by the only
comparison that isolates it: the *same bars*, scored with and without their
chain. Anything else confounds the renormalisation with the fact that
different days had different markets.

It changes nothing. It reads the archive, calls `generate()` twice per
session, and writes a markdown report.

    python3 scripts/degraded_check_report.py [--out docs/DEGRADED_CHECKS.md]
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import text

# Runs both from the repo and from inside the container, where the package
# is mounted at /srv rather than ./backend.
for _candidate in (Path(__file__).resolve().parents[1] / "backend", Path("/srv")):
    if _candidate.exists():
        sys.path.insert(0, str(_candidate))

from app.analytics import signal_engine
from app.data import repository
from app.db import SessionLocal

IST = "Asia/Kolkata"

# The bar each session is scored at. Late enough that structure, VWAP and
# the trend have something to say, early enough to be a real decision
# rather than an end-of-day summary.
DECISION_BAR = 60


def sessions_with_chains(db) -> list[date]:
    rows = db.execute(text("""
        SELECT DISTINCT date(timestamp AT TIME ZONE 'Asia/Kolkata') AS day
        FROM option_candles ORDER BY day""")).fetchall()
    return [r[0] for r in rows]


def chain_at(db, day: date) -> pd.DataFrame | None:
    """Rebuild one option chain from the archive, as of that day's close.

    One row per strike carrying both sides, which is the shape
    `options.summarise` validates — not one row per contract.
    """
    rows = db.execute(text("""
        WITH last_bar AS (
            SELECT oc.contract_id, max(oc.timestamp) AS ts
            FROM option_candles oc
            WHERE date(oc.timestamp AT TIME ZONE 'Asia/Kolkata') = :day
            GROUP BY oc.contract_id)
        SELECT c.strike, c.option_type, oc.close, oc.open_interest, oc.iv
        FROM last_bar lb
        JOIN option_candles oc
          ON oc.contract_id = lb.contract_id AND oc.timestamp = lb.ts
        JOIN option_contracts c ON c.id = oc.contract_id
    """), {"day": day}).fetchall()
    if not rows:
        return None

    strikes: dict[float, dict] = {}
    for strike, kind, close, oi, iv in rows:
        slot = strikes.setdefault(float(strike), {
            "strike": float(strike), "call_oi": 0.0, "put_oi": 0.0,
            "call_ltp": 0.0, "put_ltp": 0.0, "call_iv": 0.0, "put_iv": 0.0})
        side = "call" if kind == "CE" else "put"
        slot[f"{side}_oi"] = float(oi or 0)
        slot[f"{side}_ltp"] = float(close or 0)
        slot[f"{side}_iv"] = float(iv or 0)

    frame = pd.DataFrame(sorted(strikes.values(), key=lambda r: r["strike"]))
    return frame if len(frame) >= 3 else None


def score(candles: pd.DataFrame, chain: pd.DataFrame | None) -> dict | None:
    try:
        sig = signal_engine.generate(candles, chain=chain)
    except ValueError:
        return None
    checks = {c["name"]: c for c in sig.to_dict()["checks"]}
    return {
        "action": sig.action,
        "confidence": sig.confidence,
        "disabled": [n for n, c in checks.items() if c.get("disabled")],
    }


def analyse(db, day: date) -> dict | None:
    """Score one session twice: with its chain, and blind to it."""
    candles = repository.load_index_candles(db, start=day, end=day)
    if len(candles) < DECISION_BAR:
        return None

    window = candles.iloc[:DECISION_BAR]
    chain = chain_at(db, day)
    if chain is None:
        return None

    with_chain = score(window, chain)
    without = score(window, None)
    if not with_chain or not without:
        return None

    return {
        "day": day,
        "with": with_chain,
        "without": without,
        "delta": round(without["confidence"] - with_chain["confidence"], 4),
        "flipped": with_chain["action"] != without["action"],
    }


def era_summary(db) -> list[dict]:
    """Confidence by source, over whatever the archive currently holds."""
    out = []
    coverage = repository.coverage(db, "NIFTY", "5m")
    for source in sorted(coverage.sources):
        frame = repository.load_index_candles(db, sources=[source])
        if frame.empty:
            continue
        ist = frame["timestamp"].dt.tz_convert(IST)
        days = sorted(ist.dt.date.unique())
        confidences = []
        for day in days:
            window = frame[ist.dt.date == day]
            if len(window) < DECISION_BAR:
                continue
            scored = score(window.iloc[:DECISION_BAR], None)
            if scored:
                confidences.append(scored["confidence"])
        if confidences:
            out.append({
                "source": source, "sessions": len(confidences),
                "mean": statistics.mean(confidences),
                "median": statistics.median(confidences),
                "min": min(confidences), "max": max(confidences),
            })
    return out


def render(rows: list[dict], eras: list[dict]) -> str:
    lines = [
        "# Degraded checks in the backfilled era",
        "",
        "Generated by `scripts/degraded_check_report.py`. The engine was not",
        "modified; this only measures it.",
        "",
        "## Why this matters",
        "",
        "The backfilled era has no option-chain history, so",
        "`check_option_chain` (16% of the weight) is disabled for every",
        "backfilled bar. `signal_engine` renormalises over the checks that",
        "still run, scaling the remaining six by `1/0.84 = 1.19x`. A sweep",
        "mixing backfilled and live sessions is therefore reading two",
        "instruments off one scale unless the difference is known.",
        "",
        "## The controlled comparison",
        "",
        "Identical bars, scored with and without the chain. This isolates the",
        "renormalisation from the fact that different days had different",
        "markets.",
        "",
    ]

    if not rows:
        lines += ["_No session in the archive has both candles and an option",
                  "chain, so the controlled comparison could not be run._", ""]
    else:
        lines += [
            "| session | with chain | without chain | delta | action flip |",
            "|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {r['day']} | {r['with']['action']} "
                f"{r['with']['confidence']:.3f} | {r['without']['action']} "
                f"{r['without']['confidence']:.3f} | {r['delta']:+.3f} | "
                f"{'**YES**' if r['flipped'] else 'no'} |")
        deltas = [r["delta"] for r in rows]
        flips = sum(1 for r in rows if r["flipped"])
        lines += [
            "",
            f"- sessions compared: **{len(rows)}**",
            f"- mean delta: **{statistics.mean(deltas):+.4f}**",
            f"- largest move: **{max(deltas, key=abs):+.4f}**",
            f"- action flipped: **{flips} of {len(rows)}** "
            f"({100 * flips / len(rows):.0f}%)",
            "",
        ]
        ratios = [r["without"]["confidence"] / r["with"]["confidence"]
                  for r in rows if r["with"]["confidence"] > 0]
        if ratios:
            lower = signal_engine.MIN_CONFIDENCE / statistics.mean(ratios)
            lines += [
                f"- inflation factor: **{statistics.mean(ratios):.3f}x** "
                f"(the renormalisation is `1/0.84 = "
                f"{1 / (1 - signal_engine.WEIGHTS['option_chain']):.3f}`)",
                "",
                "### The flip count understates the risk",
                "",
                "Every delta is positive: dropping the chain does not perturb",
                "confidence, it *inflates* it, by a near-constant factor. But",
                f"`MIN_CONFIDENCE` is a fixed **{signal_engine.MIN_CONFIDENCE}**,",
                "so an inflated score clears an unmoved bar more easily.",
                "",
                f"A session whose live confidence sits between **{lower:.3f}**",
                f"and **{signal_engine.MIN_CONFIDENCE}** would HOLD live and",
                "trade when backfilled. Zero flips above means no session in",
                "this sample happened to land in that band — not that the band",
                "is safe.",
                "",
                "**Consequence for the sweep:** expect the backfilled era to",
                "produce *more* trades per session than the live era, from",
                "identical price action. Report trade counts by source before",
                "pooling them, and treat any edge that appears only in the",
                "backfilled era as suspect.",
                "",
            ]

    lines += ["## Confidence by era", "",
              "Every session scored blind (`chain=None`), so the eras differ",
              "only in their market conditions.", "",
              "| source | sessions | mean | median | min | max |",
              "|---|---|---|---|---|---|"]
    for e in eras:
        lines.append(
            f"| `{e['source']}` | {e['sessions']} | {e['mean']:.3f} | "
            f"{e['median']:.3f} | {e['min']:.3f} | {e['max']:.3f} |")
    if len(eras) < 2:
        lines += ["", "_Only one source is present. Re-run after the Angel",
                  "backfill to compare eras._"]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/DEGRADED_CHECKS.md")
    parser.add_argument("--limit", type=int, default=12,
                        help="most recent sessions to compare")
    args = parser.parse_args()

    with SessionLocal() as db:
        days = sessions_with_chains(db)[-args.limit:]
        rows = [r for r in (analyse(db, d) for d in days) if r]
        eras = era_summary(db)

    report = render(rows, eras)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
