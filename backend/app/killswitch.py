"""The kill switch, read now rather than at boot.

`risk_config()` claimed that "a kill switch flipped in the environment takes
effect on the next request, not the next deployment". It did not. It rebuilt
`RiskConfig` on every call, but from `get_settings()`, which is `@lru_cache`d
— so the value was frozen for the lifetime of the process and the only way
to engage the switch was a restart. A control documented as immediate and
implemented as deferred is worse than one honestly labelled slow: it gets
reached for in the moment it is needed and appears to do nothing.

Only this one setting is live. Everything else stays cached, because a
process whose capital or lot size can change halfway through a decision is
harder to reason about, not easier — and none of those are things you reach
for in an emergency.

Precedence mirrors pydantic-settings, so the switch behaves the same way the
rest of the configuration does:

    environment variable  >  .env file  >  the value read at boot

Failure is deliberately asymmetric. A value that is present but unreadable
engages the switch: someone was trying to say something about trading and we
could not parse it, and the safe reading of an ambiguous stop instruction is
stop. A value that is simply absent is not ambiguous, and falls back to the
boot setting.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .config import get_settings

log = logging.getLogger(__name__)

ENV_VAR = "KILL_SWITCH"

# The same spellings pydantic-settings accepts for a bool, so the switch does
# not read one way in `.env` and another way here.
_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}

# `.env` sits at the repo root, three levels up from this file
# (backend/app/killswitch.py). The container copies only `app/`, so the file
# is usually absent there and the environment carries the value instead.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

_lock = threading.Lock()
_cached: tuple[float, bool | None] = (0.0, None)   # (mtime, parsed value)


def _parse(raw: str) -> bool:
    value = raw.strip().strip('"').strip("'").lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{ENV_VAR}={raw!r} is not a boolean")


def _from_file() -> bool | None:
    """The switch as written in `.env`, or None if it says nothing about it.

    Re-read only when the file's mtime moves, so a per-request call costs one
    `stat` in the ordinary case rather than a parse.
    """
    global _cached
    try:
        mtime = _ENV_FILE.stat().st_mtime
    except OSError:
        return None

    with _lock:
        if _cached[0] == mtime:
            return _cached[1]

    parsed: bool | None = None
    try:
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, raw = line.partition("=")
            if name.strip() == ENV_VAR:
                parsed = _parse(raw)
    except ValueError as exc:
        log.error("%s — engaging the kill switch because the intent is unclear.", exc)
        parsed = True
    except OSError as exc:
        log.warning("could not read %s (%s); falling back to the boot setting.",
                    _ENV_FILE, exc)
        parsed = None

    with _lock:
        _cached = (mtime, parsed)
    return parsed


def engaged() -> bool:
    """Is the kill switch on, right now?

    Cheap enough to call on every request: an environment lookup, and a
    `stat` on a file that almost never changes.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is not None and raw.strip() != "":
        try:
            return _parse(raw)
        except ValueError as exc:
            log.error("%s — engaging the kill switch because the intent is unclear.",
                      exc)
            return True

    from_file = _from_file()
    if from_file is not None:
        return from_file

    try:
        return get_settings().kill_switch
    except Exception as exc:
        # The last resort could not answer — pydantic rejects `KILL_SWITCH=`
        # with an empty value, for instance, which also stops the app booting
        # at all. Whatever the cause, "we cannot determine whether trading is
        # meant to be halted" resolves the same way as an unreadable value.
        log.error("could not read settings (%s) — engaging the kill switch.", exc)
        return True


def reset_cache() -> None:
    """Forget the parsed `.env` value. For tests, and for a forced re-read."""
    global _cached
    with _lock:
        _cached = (0.0, None)
