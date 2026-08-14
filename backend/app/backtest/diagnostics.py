"""Backtest diagnostics.

A backtest gives you one number: expectancy. That tells you whether the
strategy works, not why. This breaks the same trades apart along the
dimensions that usually explain a losing result.

The point is to form one hypothesis you can test, not to search for settings
that look good. With around a hundred trades you can always find a
combination that turns the backtest green — and it will be fitted to noise.
Read these tables to understand the failure, then change one thing.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _summarise(trades: list[dict]) -> dict:
    """Win rate and expectancy for one group of trades."""
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    rs = [t["r_multiple"] for t in trades]
    wins = [p for p in pnls if p > 0]
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "net_pnl": round(sum(pnls), 2),
    }


def by_confidence(trades: list[dict], edges=(0.0, 0.45, 0.55, 0.65, 1.01)) -> list[dict]:
    """Does a higher confidence score actually mean a better trade?

    This is the first question to ask of any scored strategy. If the top
    bucket is no better than the bottom, the score is not measuring
    anything and no amount of threshold tuning will help.
    """
    out = []
    for low, high in zip(edges, edges[1:], strict=False):
        group = [t for t in trades if low <= t["confidence"] < high]
        if group:
            out.append({"band": f"{low:.0%}-{min(high, 1):.0%}", **_summarise(group)})
    return out


def by_exit_reason(trades: list[dict]) -> list[dict]:
    """Where do trades end?

    Heavy on 'stop' means the stop sits inside normal noise. Heavy on
    'time' means the target is unreachable in the time allowed, and for an
    option buyer that is decay bleeding you while you wait.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[t["exit_reason"]].append(t)
    return sorted(
        ({"reason": k, **_summarise(v)} for k, v in groups.items()),
        key=lambda d: -d["trades"],
    )


def by_hour(trades: list[dict]) -> list[dict]:
    """The first and last half hour of an Indian session behave differently
    from the middle. If the losses cluster at the open, the fix may be as
    simple as not trading it."""
    groups: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        # Entry times are ISO strings in UTC; +5:30 gives IST.
        hour_utc = int(t["entry_time"][11:13])
        minute = int(t["entry_time"][14:16])
        ist_hour = (hour_utc * 60 + minute + 330) // 60 % 24
        groups[ist_hour].append(t)
    return [{"ist_hour": h, **_summarise(v)} for h, v in sorted(groups.items())]


def by_direction(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[t["direction"]].append(t)
    return [{"direction": k, **_summarise(v)} for k, v in sorted(groups.items())]


def hold_times(trades: list[dict]) -> dict:
    """How long winners live versus losers.

    Losers dying much faster than winners is the signature of a stop that is
    too tight: price wobbles through it before the idea has a chance.
    """
    def minutes(t: dict) -> float:
        from datetime import datetime
        a = datetime.fromisoformat(t["entry_time"])
        b = datetime.fromisoformat(t["exit_time"])
        return (b - a).total_seconds() / 60

    if not trades:
        return {}
    wins = [minutes(t) for t in trades if t["pnl"] > 0]
    losses = [minutes(t) for t in trades if t["pnl"] <= 0]
    return {
        "avg_minutes_winners": round(sum(wins) / len(wins), 1) if wins else None,
        "avg_minutes_losers": round(sum(losses) / len(losses), 1) if losses else None,
        "note": "Losers much shorter than winners suggests the stop is inside "
                "normal noise rather than beyond it.",
    }


def decay_share(trades: list[dict]) -> dict:
    """How much of the damage is time decay rather than direction?

    Separating these matters: decay is fixed by holding shorter or choosing
    a different strike, while a bad direction call needs a better signal.
    Confusing the two sends you fixing the wrong thing.
    """
    if not trades:
        return {}
    total_decay = sum(t["decay_cost"] for t in trades)
    net = sum(t["pnl"] for t in trades)
    return {
        "total_decay_cost": round(total_decay, 2),
        "net_pnl": round(net, 2),
        "loss_without_decay": round(net + total_decay, 2),
        "note": "If the loss survives removing decay, the signal itself is "
                "the problem and option mechanics are only making it worse.",
    }


def by_check(trades: list[dict]) -> list[dict]:
    """Does each individual check predict anything?

    For every check, split the trades by whether that check pushed the
    decision the way it was taken, and compare expectancy. A check that
    helps should show a clear gap. A check with no gap is contributing
    noise, and noise with a weight attached is worse than no check at all —
    it dilutes the checks that do work.

    `edge` is the difference in expectancy between agreeing and disagreeing
    trades. Positive means the check adds something. Around zero means it
    does not. Negative means it is actively misleading you.
    """
    names: set[str] = set()
    for t in trades:
        names.update(t.get("checks", {}))

    rows = []
    for name in sorted(names):
        agreed, disagreed = [], []
        for t in trades:
            contribution = t.get("checks", {}).get(name)
            if contribution is None:
                continue
            # The trade direction: BUY is a positive-leaning decision.
            wanted = 1 if t["direction"] == "BUY" else -1
            if contribution * wanted > 0:
                agreed.append(t)
            elif contribution * wanted < 0:
                disagreed.append(t)

        a, dis = _summarise(agreed), _summarise(disagreed)
        edge = None
        if a.get("trades") and dis.get("trades"):
            edge = round(a["expectancy_r"] - dis["expectancy_r"], 3)

        rows.append({
            "check": name,
            "agreed": a,
            "disagreed": dis,
            "edge": edge,
        })

    return sorted(rows, key=lambda r: -(r["edge"] if r["edge"] is not None else -99))


def report(result: dict[str, Any]) -> dict:
    """Everything above, from one backtest result."""
    trades = result.get("trades", [])
    if not trades:
        return {"note": "No trades to diagnose."}

    return {
        "overall": result.get("stats", {}),
        "by_confidence": by_confidence(trades),
        "by_exit_reason": by_exit_reason(trades),
        "by_direction": by_direction(trades),
        "by_check": by_check(trades),
        "by_hour": by_hour(trades),
        "hold_times": hold_times(trades),
        "decay": decay_share(trades),
        "how_to_read": [
            "If the top confidence band is no better than the bottom, the "
            "score is not measuring anything — fix the checks, not the "
            "threshold.",
            "If most exits are stops and losers are much shorter than "
            "winners, the stop is inside normal noise.",
            "If removing decay still leaves a loss, the direction call is "
            "wrong and option mechanics are a second, separate problem.",
            "In by_check, a positive edge means that check helped. Around "
            "zero means it is contributing noise, which dilutes the checks "
            "that work. Negative means it is misleading you.",
            "Around a hundred trades is enough to form a hypothesis and not "
            "enough to confirm one. Change one thing, then gather more.",
        ],
    }
