"""The API's shutdown deadline — one number, shared by the API and the scripts.

    python -m app.shutdown_policy     grace=10  drain=120  margin=15  deadline=145

On SIGTERM uvicorn stops accepting connections, gives the open ones `grace`
seconds to close (a dashboard WebSocket otherwise holds the shutdown open
indefinitely), then runs the lifespan shutdown, which drains the writers for
up to `drain` seconds (migration_guard.release_after_drain) and either
releases the writer lease or ends the process with exit 70. `margin` covers
the rest of the exit.

scripts/start.sh passes `grace` to uvicorn and records `deadline` for this
API process; scripts/stop.sh waits that long for it to exit before it would
ever SIGKILL, and stops PostgreSQL and Redis only once it has. All three
come from Settings (Pass 2E-B).

Settings reads `.env` relative to the working directory, so the policy is
only the API's own if it is read from where the API runs: start.sh reads it
from the project root, as it launches the API. And the API checks it
itself: whoever will SIGKILL it after a fixed time — stop.sh, or Docker's
stop_grace_period — passes that time as QUANTDESK_STOP_DEADLINE_SECONDS, and
the API refuses to start if its own deadline would not fit inside it
(`check_supervisor`, called before the writer lease is taken).
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass

SUPERVISOR_ENV = "QUANTDESK_STOP_DEADLINE_SECONDS"


class ShutdownPolicyError(RuntimeError):
    """The shutdown settings are invalid, or longer than the supervisor waits."""


@dataclass(frozen=True)
class Policy:
    grace: int
    drain: float
    margin: int

    @property
    def deadline(self) -> int:
        """Whole seconds from SIGTERM by which the API must have exited."""
        return self.grace + math.ceil(self.drain) + self.margin


def policy(settings=None) -> Policy:
    if settings is None:
        from .config import get_settings
        settings = get_settings()
    grace = settings.shutdown_connection_grace_seconds
    drain = settings.shutdown_drain_seconds
    margin = settings.shutdown_exit_margin_seconds
    for name, value in (("connection grace", grace), ("drain", drain), ("exit margin", margin)):
        if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
            raise ShutdownPolicyError(f"shutdown {name} must be a positive number of seconds")
    return Policy(grace=math.ceil(grace), drain=float(drain), margin=math.ceil(margin))


def check_supervisor(settings=None, environ=None) -> Policy:
    """The effective policy, checked against the supervisor's deadline.

    Raises ShutdownPolicyError if the settings are invalid, or if the
    supervisor has said how long it waits (QUANTDESK_STOP_DEADLINE_SECONDS)
    and that is shorter than this API's own deadline — it would be killed
    mid-drain. Without the variable (a run under a bare uvicorn), only the
    settings are checked."""
    p = policy(settings)
    raw = (os.environ if environ is None else environ).get(SUPERVISOR_ENV)
    if raw is None:
        return p
    try:
        supervisor = int(raw)
    except ValueError:
        raise ShutdownPolicyError(f"{SUPERVISOR_ENV} must be whole seconds") from None
    if supervisor < p.deadline:
        raise ShutdownPolicyError(
            f"shutdown deadline {p.deadline}s (grace {p.grace} + drain {math.ceil(p.drain)} "
            f"+ margin {p.margin}) exceeds the {supervisor}s the supervisor waits before "
            "SIGKILL; shorten the shutdown settings or lengthen the supervisor's wait")
    return p


def main() -> int:
    try:
        p = policy()
    except Exception as exc:  # noqa: BLE001 — a settings error may quote .env
        print(f"shutdown policy unreadable: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"grace={p.grace}\ndrain={math.ceil(p.drain)}\nmargin={p.margin}\n"
          f"deadline={p.deadline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
