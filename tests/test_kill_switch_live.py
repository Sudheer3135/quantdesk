"""The kill switch takes effect now, not at the next deployment.

`risk_config()` promised exactly that — "a kill switch flipped in the
environment takes effect on the next request, not the next deployment" — and
did not deliver it. It rebuilt `RiskConfig` on every call, but from
`get_settings()`, which is `@lru_cache`d, so the value was frozen for the
life of the process. Demonstrated in the running container: flipping
`KILL_SWITCH` and calling `risk_config()` again returned the boot value.

A control documented as immediate and implemented as deferred is worse than
one honestly labelled slow. It gets reached for in the one moment it matters
and appears to do nothing.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import killswitch
from app.analytics.signal_engine import Signal
from app.config import get_settings
from app.deps import risk_config
from app.models import TradeRecord
from app.risk import live as risk_live


@pytest.fixture(autouse=True)
def clean_switch(monkeypatch, tmp_path):
    """Start each test with no switch anywhere and no parsed `.env` cached.

    The repo's own `.env` is pointed away from, so a developer who has the
    switch on locally does not silently pass tests that assert it is off.
    """
    monkeypatch.delenv(killswitch.ENV_VAR, raising=False)
    monkeypatch.setattr(killswitch, "_ENV_FILE", tmp_path / ".env")
    killswitch.reset_cache()
    get_settings.cache_clear()
    yield
    killswitch.reset_cache()
    get_settings.cache_clear()


def a_buy():
    return Signal(
        symbol="NIFTY", timeframe="5m", timestamp=datetime.now(UTC).isoformat(),
        action="BUY", confidence=0.62, price=24_200.0,
        entry=24_200.0, stop_loss=24_190.0, target=24_225.0,
        risk_reward=2.5, checks=[], context={},
    )


# ---- the reader -------------------------------------------------------

def test_the_switch_is_off_when_nothing_says_otherwise():
    assert killswitch.engaged() is False
    assert risk_config().kill_switch is False


def test_flipping_the_environment_takes_effect_on_the_next_call(monkeypatch):
    """The regression. This used to require restarting the process."""
    assert risk_config().kill_switch is False        # boot state

    monkeypatch.setenv(killswitch.ENV_VAR, "true")
    assert risk_config().kill_switch is True         # next call, no restart

    monkeypatch.setenv(killswitch.ENV_VAR, "false")
    assert risk_config().kill_switch is False        # and back again


def test_the_cached_settings_object_is_not_what_answers(monkeypatch):
    """Pins the actual cause. `get_settings()` is memoised and stays that
    way — the switch has to be read outside it."""
    before = get_settings()
    monkeypatch.setenv(killswitch.ENV_VAR, "true")
    after = get_settings()

    assert before is after, "test premise: settings should still be cached"
    assert after.kill_switch is False, "the cached object cannot see the flip"
    assert killswitch.engaged() is True, "but the live reader can"


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "on", " true "])
def test_spellings_that_mean_on(monkeypatch, raw):
    monkeypatch.setenv(killswitch.ENV_VAR, raw)
    assert killswitch.engaged() is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "off"])
def test_spellings_that_mean_off(monkeypatch, raw):
    monkeypatch.setenv(killswitch.ENV_VAR, raw)
    assert killswitch.engaged() is False


def test_an_unreadable_value_engages_the_switch(monkeypatch, caplog):
    """Asymmetric on purpose. Someone was trying to say something about
    trading and we could not parse it; the safe reading of an ambiguous stop
    instruction is stop."""
    monkeypatch.setenv(killswitch.ENV_VAR, "banana")
    assert killswitch.engaged() is True
    assert "kill switch" in caplog.text.lower()


def test_an_empty_value_fails_safe(monkeypatch, caplog):
    """`KILL_SWITCH=` looks like "unset" in a shell, but pydantic rejects it
    as a boolean — so the app would not boot with it either. The reader must
    not crash on the way to finding that out, and an undeterminable switch
    resolves the same way as an unreadable one."""
    monkeypatch.setenv(killswitch.ENV_VAR, "")
    assert killswitch.engaged() is True
    assert "kill switch" in caplog.text.lower()


# ---- the .env file ----------------------------------------------------

def test_the_env_file_is_read_and_re_read(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(killswitch, "_ENV_FILE", env)

    env.write_text("BROKER=mock\nKILL_SWITCH=true\n")
    killswitch.reset_cache()
    assert killswitch.engaged() is True

    # A second edit must be seen. The mtime is nudged because a same-second
    # rewrite can land on an identical timestamp on a coarse filesystem.
    env.write_text("BROKER=mock\nKILL_SWITCH=false\n")
    import os
    future = datetime.now(UTC) + timedelta(seconds=2)
    os.utime(env, (future.timestamp(), future.timestamp()))
    assert killswitch.engaged() is False


def test_the_environment_outranks_the_file(monkeypatch, tmp_path):
    """Same precedence pydantic-settings uses, so the switch does not behave
    one way in `.env` and another way here."""
    env = tmp_path / ".env"
    env.write_text("KILL_SWITCH=false\n")
    monkeypatch.setattr(killswitch, "_ENV_FILE", env)
    killswitch.reset_cache()

    monkeypatch.setenv(killswitch.ENV_VAR, "true")
    assert killswitch.engaged() is True


def test_a_commented_out_switch_is_not_a_setting(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# KILL_SWITCH=true\n")
    monkeypatch.setattr(killswitch, "_ENV_FILE", env)
    killswitch.reset_cache()
    assert killswitch.engaged() is False


def test_a_missing_file_falls_back_to_the_boot_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(killswitch, "_ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setenv("KILL_SWITCH", "true")
    get_settings.cache_clear()
    boot = get_settings()
    assert boot.kill_switch is True

    monkeypatch.delenv("KILL_SWITCH")
    killswitch.reset_cache()
    assert killswitch.engaged() is True, "boot value is the last resort"


# ---- end to end, through a real decision ------------------------------

def test_boot_false_then_true_blocks_the_next_decision(db, monkeypatch):
    """The behaviour asked for: off at boot, flipped on, next request blocks."""
    assert risk_live.decide(db, a_buy())["state"] == "approved"

    monkeypatch.setenv(killswitch.ENV_VAR, "true")

    blocked = risk_live.decide(db, a_buy())
    assert blocked["state"] == "blocked"
    assert blocked["approved"] is False
    assert any("Kill switch" in r for r in blocked["reasons"])


def test_true_then_false_allows_again_when_nothing_else_blocks(db, monkeypatch):
    monkeypatch.setenv(killswitch.ENV_VAR, "true")
    assert risk_live.decide(db, a_buy())["state"] == "blocked"

    monkeypatch.setenv(killswitch.ENV_VAR, "false")

    allowed = risk_live.decide(db, a_buy())
    assert allowed["state"] == "approved"
    assert allowed["approved"] is True
    assert allowed["quantity"] > 0


def test_releasing_the_switch_does_not_override_the_other_limits(db, monkeypatch):
    """Turning the switch off means "stop overriding", not "approve"."""
    db.add(TradeRecord(symbol="NIFTY", side="BUY", quantity=75, entry=24_200.0,
                       stop_loss=24_190.0, status="open",
                       created_at=datetime.now(UTC)))
    db.commit()

    monkeypatch.setenv(killswitch.ENV_VAR, "false")
    decision = risk_live.decide(db, a_buy())

    assert decision["state"] == "blocked"
    assert any("Already holding" in r for r in decision["reasons"])
