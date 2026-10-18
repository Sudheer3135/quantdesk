"""The reward:risk floor against the rounding the signal engine does.

The engine builds every trade as exactly 1:2, then rounds the stop and the
target to two decimals and leaves the entry at the raw price. The risk
manager compared the result against 2.0 with a 1e-6 tolerance, which is float
noise, not rounding. Measured on 14-Sep-2026: 37 of 630 trade signals were
refused as "1:2.00, below the 1:2.0 floor", every one between 1.99936 and
1.99996 — trades that met the rule, refused for how they were printed.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import signal_engine
from app.risk.manager import DayState, RiskConfig, evaluate

# Plenty of capital, so the only rule that can refuse is the RR floor.
CFG = RiskConfig(capital=10_000_000, lot_size=75)


def rr_reasons(entry, stop, target):
    d = evaluate(config=CFG, state=DayState(trading_day=date(2026, 9, 15)),
                 entry=entry, stop_loss=stop, target=target)
    # An approved trade also carries an informational "Reward:risk 1:2.00."
    # note, so match the refusal wording rather than the label.
    return [r for r in d.reasons if "below the" in r]


def engine_levels(price, atr, action):
    """Exactly what `signal_engine.generate` does to build its levels."""
    risk = max(atr * 1.2, price * 0.0008)
    if action == "BUY":
        return price, round(price - risk, 2), round(price + risk * 2.0, 2)
    return price, round(price + risk, 2), round(price - risk * 2.0, 2)


def test_the_trade_from_the_screenshot_is_not_refused():
    """SELL at 24,023.70 / stop 24,048.96 / target 23,973.18, refused on the
    desk as "1:2.00, below the 1:2.0 floor". The entry is shown rounded; the
    engine holds it raw, which is what put the RR a hair under two."""
    assert rr_reasons(24023.6951, 24048.96, 23973.18) == []


@pytest.mark.parametrize("rr", [1.99936, 1.99950, 1.99996])
def test_the_measured_refusals_pass(rr):
    """The span observed across all 37 refused signals."""
    entry, risk = 24000.0, 25.26
    assert rr_reasons(entry, entry - risk, entry + risk * rr) == []


def test_every_level_the_engine_can_build_passes_the_floor():
    """Sweep prices across a hundredth and ATRs across a normal range: the
    engine's own 1:2 must never be refused by its own risk manager."""
    refused = []
    for cents in range(0, 100):
        price = 23000 + cents / 100 + 0.0049
        for atr in (8.0, 12.78, 16.0, 20.58, 25.26, 40.0, 80.0):
            for action in ("BUY", "SELL"):
                if rr_reasons(*engine_levels(price, atr, action)):
                    refused.append((price, atr, action))
    assert not refused, f"{len(refused)} engine-built 1:2 trades refused, e.g. {refused[:3]}"


@pytest.mark.parametrize("rr", [1.99, 1.98, 1.9, 1.5])
def test_a_trade_genuinely_short_of_the_floor_is_still_refused(rr):
    """The allowance is rounding, not generosity."""
    entry, risk = 24000.0, 25.0
    assert rr_reasons(entry, entry - risk, entry + risk * rr)


def test_the_allowance_shrinks_as_the_stop_widens():
    """On a 500-point stop, rounding cannot move the RR by 0.001 — so a
    1.999 there is a real shortfall and is refused."""
    entry, risk = 24000.0, 500.0
    assert rr_reasons(entry, entry - risk, entry + risk * 1.999)


def test_a_refusal_does_not_contradict_itself():
    """At two decimals a refused 1.999 printed as '1:2.00, below the 1:2.0
    floor'. Three decimals keeps the reason honest."""
    entry, risk = 24000.0, 500.0
    (reason,) = rr_reasons(entry, entry - risk, entry + risk * 1.999)
    assert "1:1.999" in reason


def test_the_engine_still_targets_two():
    """If this ever changes, the sweep above has to be revisited."""
    import inspect
    assert inspect.signature(signal_engine.generate).parameters["rr_target"].default == 2.0
