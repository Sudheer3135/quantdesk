"""Shared clocks, position ledger and deterministic research provenance."""
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import platform
import pandas as pd


def bar_close(stamp, minutes=5):
    return pd.Timestamp(stamp) + pd.Timedelta(minutes=minutes)


class State(str, Enum):
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
            'clock': 'timestamps are bar opens; features available at bar close',
            'execution_accuracy': 'bar-resolution estimated fills, not tick-accurate'}
