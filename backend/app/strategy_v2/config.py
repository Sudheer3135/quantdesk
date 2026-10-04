"""Every number strategy v2 runs on, in one object.

Agreed with the desk owner on 14-Sep-2026: intraday only, weekly expiries,
India VIX as a gate, run on paper with a simulated account before any real
money. The account is simulated because a real ₹10–20k cannot hold one NIFTY
weekly lot inside a 1% risk rule — measured against the archive, one
at-the-money lot cost ₹4,300–12,400 four to six days from expiry — and a
strategy that has to break its own risk rules to trade at all has not been
tested, it has been gambled.

None of these is derived from a backtest yet. They are a starting position
to be measured, and the paper record exists to measure them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import time

from ..analytics import plan as plan_builder

NAME = "v2"
VERSION = "2.0"


@dataclass(frozen=True)
class V2Config:
    # ---- the simulated account ----------------------------------------
    paper_capital: float = 350_000.0

    # ---- risk: the desk's rulebook, unchanged ---------------------------
    # Passed to `risk.manager.evaluate` as they are. v2 does not own a risk
    # rule; it owns the numbers it hands the one the desk already has.
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 2
    max_open_positions: int = 1
    max_capital_deployed_pct: float = 20.0

    # ---- the decision it accepts ----------------------------------------
    require_bias_agreement: bool = True
    require_entry_states: tuple[str, ...] = (plan_builder.ENTER_NOW,)
    # A signal older than this is a decision about a market that has moved.
    max_signal_age_seconds: float = 420.0

    # ---- when ----------------------------------------------------------
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 30)
    session_exit: time = time(15, 15)
    max_minutes_in_trade: int = 120
    # NIFTY's weekly expiry day moves on its own flows. No new position is
    # opened on one, whichever contract it would buy.
    no_entry_on_expiry_day: bool = True

    # ---- which contract -------------------------------------------------
    # Sessions strictly after today up to and including expiry. 2 rules out
    # expiry day and the day before, where gamma prices the option off the
    # clock rather than off the signal.
    min_sessions_to_expiry: int = 2
    target_delta: float = 0.50
    min_delta: float = 0.45
    max_delta: float = 0.60
    min_premium: float = 20.0
    max_spread_pct: float = 5.0
    max_quote_age_seconds: float = 10.0

    # ---- how it closes ------------------------------------------------------
    # Caps on the index-derived levels, not replacements for them: the stop
    # is whichever comes first of the index stop and a 30% premium loss, the
    # target whichever comes first of the index target and a 50% gain.
    premium_stop_pct: float = 30.0
    premium_target_pct: float = 50.0

    # ---- India VIX gate -----------------------------------------------------
    vix_lookback_sessions: int = 252
    vix_min_history: int = 120
    vix_block_percentile: float = 80.0
    vix_spike_pct: float = 10.0

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("entry_start", "entry_end", "session_exit"):
            out[key] = getattr(self, key).strftime("%H:%M")
        out["require_entry_states"] = list(self.require_entry_states)
        out["name"], out["version"] = NAME, VERSION
        return out


DEFAULT = V2Config()


@dataclass
class Rejection:
    code: str
    detail: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, **self.data}
