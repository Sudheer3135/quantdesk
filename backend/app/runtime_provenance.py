"""The repository state this API application initialized on (Day 1 provenance).

A signal row's `code_id` is sampled when the row is written, so it moves
whenever the repository on disk moves — a frontend edit or a commit made
while the backend keeps running changes it. Measured 01-Oct-2026: one API
process started at 09:16 wrote rows labelled `51c1dc0`, five different
`51c1dc0+dirty.*` and `12c2935` as the tree was edited around it.

That row-time value is kept exactly as it is. This module records a second,
separate value: one sample of the same `git_state()`, taken once when the
FastAPI lifespan starts and before any scheduled writer runs, and then held
unchanged for the rest of that run. It says what was on disk when this
application initialized. It is not proof of the code image in memory —
modules imported later read whatever is on disk then — and the pid beside it
is supporting detail, not a unique identity for the run.

Never sampled lazily. A process that did not initialize the API (a script,
a test without the lifespan) reports `not_captured`, not today's repository.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime

SCHEMA = "api_init_provenance/1"
CAPTURED = "captured"
GIT_UNAVAILABLE = "git_unavailable"
NOT_CAPTURED = "not_captured"
STATUSES = (CAPTURED, GIT_UNAVAILABLE, NOT_CAPTURED)

BASIS = ("repository git state observed once when this API application "
         "initialized (start of the FastAPI lifespan, before scheduled writers), "
         "held unchanged for that run; not proof of the Python code image in "
         "memory, since modules imported later read the disk as it is then; the "
         "pid is supporting detail from this host, not a unique runtime identity")

# Declared fields, in serialised order. The evidence copies only these.
FIELDS = ("schema", "status", "repo_state", "code_id_at_init", "pid",
          "initialized_at", "basis", "reason")
REPO_STATE_FIELDS = ("git_commit", "dirty_worktree", "dirty_diff_sha256", "note")

# Held as JSON text so no caller can reach in and change it.
_snapshot: str | None = None


def _freeze(record: dict) -> str:
    return json.dumps({k: record.get(k) for k in FIELDS})


def initialize(*, state_fn=None, now: datetime | None = None,
               pid: int | None = None) -> dict:
    """Take this run's snapshot. Called once, from the API lifespan.

    Never raises: a repository that cannot be read is recorded as such, and
    provenance is observation only — it must not stop the desk.
    """
    global _snapshot
    from .backtest import measurement

    state_fn = state_fn or measurement.git_state
    record = {"schema": SCHEMA, "pid": pid if pid is not None else os.getpid(),
              "initialized_at": (now or datetime.now(UTC)).isoformat(),
              "basis": BASIS, "reason": None}
    try:
        state = dict(state_fn())
    except Exception as exc:                            # noqa: BLE001
        state = {"git_commit": measurement.GIT_UNAVAILABLE, "dirty_worktree": None,
                 "dirty_diff_sha256": None,
                 "note": f"git state could not be read: {type(exc).__name__}"}
    record["repo_state"] = {k: state.get(k) for k in REPO_STATE_FIELDS}
    if state.get("git_commit") in (None, "", measurement.GIT_UNAVAILABLE):
        record["status"] = GIT_UNAVAILABLE
        record["code_id_at_init"] = None
        record["reason"] = "git state was unavailable when the API initialized"
    else:
        record["status"] = CAPTURED
        record["code_id_at_init"] = measurement.code_id(state)
    _snapshot = _freeze(record)
    return current()


def current() -> dict:
    """This run's snapshot, as a fresh copy. Never samples the repository."""
    if _snapshot is None:
        return json.loads(_freeze({
            "schema": SCHEMA, "status": NOT_CAPTURED, "basis": BASIS,
            "reason": "this process did not initialize the API, so no "
                      "initialization snapshot exists; none is taken later"}))
    return json.loads(_snapshot)


def _reset_for_tests() -> None:
    global _snapshot
    _snapshot = None
