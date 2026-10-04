"""Sample adequacy and session-clustered uncertainty (OS-6).

**Counts, always.** Every result carries how much evidence is behind it:
independent sessions, active sessions, closed trades, no-trade sessions,
positive and negative sessions. Sessions and trades are different counts —
forty trades on three afternoons is three observations of the market, not
forty.

**Policy minimums.** 30 independent sessions and 100 closed trades. Below
either, `sample_status` is INSUFFICIENT_SAMPLE. The result is still shown —
hiding a small sample is its own distortion — but it is labelled. These are
research-policy thresholds, not a proof that anything above them is enough.

**Uncertainty respects the session.** Trades on the same session share the
same market, so they are not independent draws. The only bootstrap here
resamples whole sessions (a cluster bootstrap); there is no trade-level
IID path, and passing trades where sessions are expected is refused. It is
seeded and reproducible, and the seed, resample count and cluster basis
travel with the result.

An interval that happens to exclude zero is reported as an interval. No
significance is claimed from it.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

MIN_INDEPENDENT_SESSIONS = 30
MIN_CLOSED_TRADES = 100
INSUFFICIENT = "INSUFFICIENT_SAMPLE"
MEETS_POLICY = "MEETS_POLICY_MINIMUM"

CLUSTER_BASIS = "IST trading session"
METHOD = "nonparametric percentile bootstrap, resampling whole sessions with replacement"
NO_SIGNIFICANCE = ("descriptive interval only; no significance is claimed, and an "
                   "interval excluding zero is not treated as a test result")


@dataclass(frozen=True)
class SessionOutcome:
    """One eligible session: its net P&L and the closed trades it held."""
    session: str
    net_pnl: float
    trade_r: tuple[float, ...] = ()
    trade_pnl: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if len(self.trade_r) != len(self.trade_pnl):
            raise ValueError("trade_r and trade_pnl must describe the same trades")


def _require_sessions(sessions) -> list[SessionOutcome]:
    rows = list(sessions)
    if any(not isinstance(s, SessionOutcome) for s in rows):
        raise TypeError("uncertainty is computed over sessions (SessionOutcome); "
                        "trade-level IID resampling is not supported")
    keys = [s.session for s in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("each session may appear once")
    return rows


def adequacy(sessions: Sequence[SessionOutcome], *, excluded_sessions: int = 0) -> dict:
    rows = _require_sessions(sessions)
    trades = sum(len(s.trade_r) for s in rows)
    reasons = []
    if len(rows) < MIN_INDEPENDENT_SESSIONS:
        reasons.append(f"{len(rows)} independent sessions < {MIN_INDEPENDENT_SESSIONS}")
    if trades < MIN_CLOSED_TRADES:
        reasons.append(f"{trades} closed trades < {MIN_CLOSED_TRADES}")
    return {
        "independent_sessions": len(rows),
        "active_trading_sessions": sum(1 for s in rows if s.trade_r),
        "closed_trades": trades,
        "no_trade_sessions": sum(1 for s in rows if not s.trade_r),
        "positive_sessions": sum(1 for s in rows if s.net_pnl > 0),
        "negative_sessions": sum(1 for s in rows if s.net_pnl < 0),
        "flat_sessions": sum(1 for s in rows if s.net_pnl == 0),
        "excluded_sessions": excluded_sessions,
        "sample_status": INSUFFICIENT if reasons else MEETS_POLICY,
        "sample_reasons": reasons,
        "policy": {"min_independent_sessions": MIN_INDEPENDENT_SESSIONS,
                   "min_closed_trades": MIN_CLOSED_TRADES,
                   "note": "research-policy thresholds, not a proof of adequacy"},
    }


def _interval(values: list[float | None], level: float) -> dict:
    undefined = sum(1 for v in values if v is None)
    if undefined:
        return {"status": "unavailable", "undefined_resamples": undefined,
                "reason": "undefined in at least one resample"}
    tail = (1 - level) / 2 * 100
    low, high = np.percentile(np.asarray(values, dtype=float), [tail, 100 - tail])
    return {"status": "ok", "low": float(low), "high": float(high)}


def _expectancy(chosen: list[SessionOutcome]) -> float | None:
    r = [x for s in chosen for x in s.trade_r]
    return float(np.mean(r)) if r else None


def _profit_factor(chosen: list[SessionOutcome]) -> float | None:
    pnl = [x for s in chosen for x in s.trade_pnl]
    losses = -sum(x for x in pnl if x < 0)
    return float(sum(x for x in pnl if x > 0) / losses) if losses > 0 else None


def session_bootstrap(sessions: Sequence[SessionOutcome], *, seed: int,
                      resamples: int = 2000, level: float = 0.95) -> dict:
    """Percentile intervals from resampling whole sessions."""
    rows = _require_sessions(sessions)
    provenance = {"seed": int(seed), "resamples": int(resamples),
                  "cluster_basis": CLUSTER_BASIS, "method": METHOD, "level": level,
                  "sessions": len(rows), "note": NO_SIGNIFICANCE}
    point = {"mean_session_net_pnl": (float(np.mean([s.net_pnl for s in rows]))
                                      if rows else None),
             "expectancy_r": _expectancy(rows), "profit_factor": _profit_factor(rows)}
    if len(rows) < 2:
        return {"point": point, "intervals": {
            k: {"status": "unavailable", "reason": "fewer than 2 sessions"}
            for k in point}, "provenance": provenance}

    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(rows), size=(resamples, len(rows)))
    mean_pnl, expectancy, pf = [], [], []
    for draw in draws:
        chosen = [rows[i] for i in draw]
        mean_pnl.append(float(np.mean([s.net_pnl for s in chosen])))
        expectancy.append(_expectancy(chosen))
        pf.append(_profit_factor(chosen))
    return {"point": point,
            "intervals": {"mean_session_net_pnl": _interval(mean_pnl, level),
                          "expectancy_r": _interval(expectancy, level),
                          "profit_factor": _interval(pf, level)},
            "provenance": provenance | {
                "resample_unit_count": len(rows),
                "draws_fingerprint": hashlib.sha256(draws.tobytes()).hexdigest()}}
