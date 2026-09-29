"""Shared clocks, position ledger and deterministic research provenance."""
import hashlib
import json
import math
import platform
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pandas as pd


def bar_close(stamp, minutes=5):
    return pd.Timestamp(stamp) + pd.Timedelta(minutes=minutes)


# str + Enum, not StrEnum: StrEnum changes str(), which feeds persisted
# ledgers and reproducibility hashes.
class State(str, Enum):  # noqa: UP042
    FLAT = 'FLAT'
    ENTRY_PENDING = 'ENTRY_PENDING'
    OPEN = 'OPEN'
    EXIT_PENDING = 'EXIT_PENDING'
    CLOSED = 'CLOSED'


@dataclass
class PositionLedger:
    state: State = State.FLAT
    events: list = field(default_factory=list)
    entries: int = 0
    exits: int = 0

    def move(self, state, timestamp, **details):
        allowed = {State.FLAT: State.ENTRY_PENDING, State.ENTRY_PENDING: State.OPEN,
                   State.OPEN: State.EXIT_PENDING, State.EXIT_PENDING: State.CLOSED,
                   State.CLOSED: State.ENTRY_PENDING}
        if allowed.get(self.state) != state:
            raise ValueError(f'invalid position transition {self.state} -> {state}')
        stamp = pd.Timestamp(timestamp)
        if self.events and stamp < pd.Timestamp(self.events[-1]['timestamp']):
            raise ValueError('position clock moved backwards')
        if state == State.OPEN:
            q = details.get('quantity', 0)
            if not math.isfinite(q) or q <= 0 or int(q) != q:
                raise ValueError('position quantity must be a positive integer')
            self.entries += 1
        if state == State.CLOSED:
            self.exits += 1
        self.state = state
        self.events.append({'state': state.value, 'timestamp': stamp.isoformat(), **details})

    def enter(self, decision_time, execution_time, quantity):
        self.move(State.ENTRY_PENDING, decision_time)
        self.move(State.OPEN, execution_time, quantity=quantity)

    def close(self, timestamp, reason, pnl):
        self.move(State.EXIT_PENDING, timestamp, reason=reason)
        self.move(State.CLOSED, timestamp, reason=reason, pnl=pnl)

    def finish(self, trades, initial, final):
        if self.entries != self.exits or self.exits != len(trades):
            raise ValueError('unreconciled position count')
        if abs(final - initial - sum(t.pnl for t in trades)) > 0.02:
            raise ValueError('capital ledger does not reconcile')
        return {'entries': self.entries, 'closed_positions': self.exits,
                'state': self.state.value, 'capital_reconciled': True, 'events': self.events}


GIT_UNAVAILABLE = "unavailable"


def _git(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          check=True, timeout=10).stdout


def git_state(root=None):
    """Which code this is, including code that has not been committed (RP-4).

    A HEAD commit alone describes a clean tree. On a dirty one it names code
    that is not the code that ran, and a result labelled with it would be
    reproducible only by luck. So a dirty tree is reported as dirty, with a
    fingerprint of exactly what differs from HEAD: the tracked diff and the
    contents of every untracked, non-ignored file. Two runs on the same
    uncommitted edit share a fingerprint; any further edit changes it.

    Research on a dirty tree is not forbidden — it is labelled.
    """
    root = Path(root) if root else Path(__file__).resolve().parents[3]
    try:
        commit = _git(root, "rev-parse", "HEAD").decode().strip()
        status = _git(root, "status", "--porcelain=v1", "-z",
                      "--untracked-files=all")
    except Exception:                                   # noqa: BLE001
        return {"git_commit": GIT_UNAVAILABLE, "dirty_worktree": None,
                "dirty_diff_sha256": None,
                "note": "git metadata could not be read; the code hash is the "
                        "only identifier of the code"}
    dirty = bool(status.strip(b"\0"))
    fingerprint = None
    if dirty:
        digest = hashlib.sha256(_git(root, "diff", "HEAD", "--binary"))
        for entry in sorted(e for e in status.split(b"\0") if e.startswith(b"?? ")):
            path = entry[3:]
            digest.update(path)
            try:
                digest.update((root / path.decode()).read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
        fingerprint = digest.hexdigest()
    return {"git_commit": commit, "dirty_worktree": dirty,
            "dirty_diff_sha256": fingerprint}


def code_id(state=None):
    """One short string naming the code: the commit, and the diff if dirty."""
    state = state or git_state()
    commit = state.get("git_commit")
    if not commit or commit == GIT_UNAVAILABLE:
        return GIT_UNAVAILABLE
    if state.get("dirty_worktree"):
        return f"{commit[:12]}+dirty.{(state.get('dirty_diff_sha256') or '')[:12]}"
    return commit[:12]


def provenance(frame, configuration, signal_fn=None):
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                         allow_nan=True).encode()).hexdigest()
    source = Path(__file__).resolve().parents[1]
    code_hash = hashlib.sha256()
    for file in sorted(source.rglob('*.py')):
        code_hash.update(str(file.relative_to(source)).encode())
        code_hash.update(file.read_bytes())
    return {'measurement_version': '2', 'data_sha256': digest(frame.to_dict('records')),
            'input_attributes': frame.attrs, 'config': configuration,
            'config_sha256': digest(configuration), 'code_sha256': code_hash.hexdigest(),
            'python': platform.python_version(), 'pandas': pd.__version__,
            'custom_signal': None if signal_fn is None else
            f'{signal_fn.__module__}.{signal_fn.__qualname__}',
            'git': git_state(),
            'clock': 'timestamps are bar opens; features available at bar close',
            'execution_accuracy': 'bar-resolution estimated fills, not tick-accurate'}
