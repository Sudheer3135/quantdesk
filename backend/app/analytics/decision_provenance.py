"""The clocks and the provenance of one decision (TC-2, RP-2).

A stored signal has to answer two questions on its own, without a backtest's
metadata or anybody's memory beside it:

  **When?** Six clocks, persisted as columns:

      bar_open_time <= bar_close_time <= available_at <= decision_at
                                                    <= earliest_execution_time

  `received_at` is when Quant Desk actually received the candles. It is
  recorded by the live path at the moment of receipt and is never derived
  from the exchange's timestamps; where nothing recorded it — a replay —
  it stays None, and `available_at` then falls back to the bar's close and
  says so in `available_basis`.

  **What made it?** The strategy version, a hash of every parameter the
  engine decided with, a fingerprint of the exact inputs it read, where the
  data came from, and which code ran. Two signals with the same parameter
  hash and input fingerprint were produced by the same rules from the same
  data; a difference in either is visible on the row.

The hashes are deterministic: canonical JSON with sorted keys for the
parameters, and fixed-width little-endian bytes for the numeric inputs, so
the same inputs hash the same on any machine and any run.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from ..backtest import execution
from . import warmup

# Bumped by hand when the engine's decision rules change meaning. The
# parameter hash catches a changed number; this names a changed rule.
STRATEGY_VERSION = "nifty-signal-engine/1"

PERSISTED = "persisted"
RECEIVED = "received_at"
BAR_CLOSE_REPLAY = "bar_close_replay"

CANDLE_FIELDS = ("open", "high", "low", "close", "volume")
CHAIN_FIELDS = ("call_oi", "put_oi", "call_oi_change", "put_oi_change",
                "call_volume", "put_volume", "call_iv", "put_iv",
                "call_ltp", "put_ltp")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parameter_hash(parameters: dict) -> str:
    return _sha(json.dumps(parameters, sort_keys=True, default=str,
                           separators=(",", ":")).encode())


def _column_bytes(frame: pd.DataFrame, name: str) -> bytes:
    if name not in frame.columns:
        return b"<absent:" + name.encode() + b">"
    values = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype="<f8")
    return name.encode() + np.ascontiguousarray(values).tobytes()


def input_fingerprint(candles: pd.DataFrame, chain: pd.DataFrame | None,
                      india_vix: float | None) -> str:
    """A hash of exactly what the engine read.

    The candles as the engine saw them — after completed-candle filtering
    and the declared-history trim — the option chain if there was one, and
    the VIX. NaN hashes as NaN, so a missing value and a zero differ.
    """
    digest = hashlib.sha256()
    stamps = pd.to_datetime(candles["timestamp"], utc=True)
    digest.update(np.ascontiguousarray(
        stamps.astype("int64").to_numpy(dtype="<i8")).tobytes())
    for name in CANDLE_FIELDS:
        digest.update(_column_bytes(candles, name))
    if chain is None:
        digest.update(b"<no-chain>")
    else:
        ordered = chain.sort_values("strike") if "strike" in chain else chain
        digest.update(_column_bytes(ordered, "strike"))
        for name in CHAIN_FIELDS:
            digest.update(_column_bytes(ordered, name))
    digest.update(b"vix:" + (repr(float(india_vix)).encode()
                             if india_vix is not None else b"none"))
    return digest.hexdigest()


def clocks(*, bar_open, timeframe_minutes: int, received_at, decision_at,
           policy: execution.ExecutionPolicy = execution.DEFAULT_POLICY) -> dict:
    """The six clocks of one decision, checked.

    Raises if the ordering is violated. A decision that claims to have read
    a bar before the bar closed, or to be executable before it was made, is
    a bug in whatever produced it, and storing it would make the bug
    permanent and invisible.
    """
    bar_open = pd.Timestamp(bar_open)
    bar_close = bar_open + pd.Timedelta(minutes=timeframe_minutes)
    decision = pd.Timestamp(decision_at)
    received = pd.Timestamp(received_at) if received_at is not None else None

    if received is not None:
        available, basis = max(bar_close, received), RECEIVED
    else:
        # Nothing recorded a receipt — a replay of stored bars. The bar is
        # taken as available at its close, and the basis says that is an
        # assumption rather than an observation.
        available, basis = bar_close, BAR_CLOSE_REPLAY
    earliest = execution.earliest_execution_time(decision, policy)

    if not bar_close <= available:
        raise ValueError("clock violation: bar_close > available_at")
    if not available <= decision:
        raise ValueError(
            f"clock violation: available_at {available.isoformat()} is after "
            f"decision_at {decision.isoformat()} — the decision read a bar it "
            "could not yet have had")
    if not earliest >= decision:
        raise ValueError("clock violation: earliest_execution_time < decision_at")

    return {
        "bar_open_time": bar_open.isoformat(),
        "bar_close_time": bar_close.isoformat(),
        "available_at": available.isoformat(),
        "available_basis": basis,
        "received_at": received.isoformat() if received is not None else None,
        "decision_at": decision.isoformat(),
        # Kept under its old name too: `evaluation.outcomes` and the audit
        # probes read `signal_time`, and renaming it would orphan every
        # stored row that uses it.
        "signal_time": decision.isoformat(),
        "earliest_execution_time": earliest.isoformat(),
        "execution_latency_seconds": policy.latency_seconds,
    }


PERSISTED_CLOCKS = ("bar_open_time", "bar_close_time", "available_at",
                    "decision_at", "earliest_execution_time")


class ClockViolation(ValueError):
    """A stored row whose persisted clocks are incomplete or out of order."""


def claims_persisted(row) -> bool:
    """Whether a stored signal says its clocks were persisted (TC-2).

    A deferred column that was never loaded is read from the database when
    the database has it — a row loaded without its provenance group is not
    thereby a legacy row. Only a database without the column (an archive
    from before migration 0009) makes the row legacy. A detached row whose
    clock basis cannot be read at all is refused rather than guessed.
    """
    import sqlalchemy as sa
    from sqlalchemy.exc import NoInspectionAvailable

    from ..data import schema

    try:
        state = sa.inspect(row)
    except NoInspectionAvailable:
        return getattr(row, "clock_basis", None) == PERSISTED
    if "clock_basis" in state.unloaded:
        if state.session is None:
            if state.transient or state.pending:
                return False
            raise ClockViolation(
                "the row's clock basis was never loaded and it is detached; "
                "whether its clocks are persisted cannot be determined")
        if not schema.has_columns(state.session, row.__tablename__, "clock_basis"):
            return False
    return row.clock_basis == PERSISTED


def persisted(row, *, timeframe_minutes: int | None = None) -> dict[str, pd.Timestamp]:
    """The persisted clocks of a stored signal, checked, as UTC timestamps.

    A reader must not trust stored clocks it has not re-checked: a row
    written by a bug, edited by hand or migrated badly is exactly the row
    whose ordering is wrong. Raises `ClockViolation` on any missing clock or
    any violation of

        bar_open < bar_close <= available_at <= decision_at <= earliest_execution_time

and, given `timeframe_minutes`, bar_close − bar_open equal to it.

    so the caller fails closed rather than falling back to a weaker clock.
    """
    stamps = {}
    for name in PERSISTED_CLOCKS:
        value = getattr(row, name, None)
        if value is None:
            raise ClockViolation(f"persisted clock {name} is missing")
        stamp = pd.Timestamp(value)
        # SQLite hands aware datetimes back naive, and they are stored UTC.
        stamps[name] = stamp.tz_localize("UTC") if stamp.tzinfo is None \
            else stamp.tz_convert("UTC")
    ordered = list(zip(PERSISTED_CLOCKS, PERSISTED_CLOCKS[1:]))
    if not stamps["bar_open_time"] < stamps["bar_close_time"]:
        raise ClockViolation("persisted bar_close_time is not after bar_open_time")
    for earlier, later in ordered[1:]:
        if not stamps[earlier] <= stamps[later]:
            raise ClockViolation(
                f"persisted {later} {stamps[later].isoformat()} precedes "
                f"{earlier} {stamps[earlier].isoformat()}")
    if timeframe_minutes is not None and \
            stamps["bar_close_time"] - stamps["bar_open_time"] \
            != pd.Timedelta(minutes=timeframe_minutes):
        raise ClockViolation(
            f"persisted bar interval is not {timeframe_minutes} minutes")
    return stamps


def record(*, parameters: dict, candles: pd.DataFrame,
           chain: pd.DataFrame | None, india_vix: float | None) -> dict:
    """The provenance block the engine attaches to a signal."""
    return {
        "strategy_version": STRATEGY_VERSION,
        "parameter_hash": parameter_hash(parameters),
        "input_fingerprint": input_fingerprint(candles, chain, india_vix),
        "input_bars": int(len(candles)),
        "warmup": warmup.describe(),
        "parameters": parameters,
    }


def _stamp(value):
    return pd.Timestamp(value).to_pydatetime() if value else None


def columns(signal, *, data_source: str | None, code_id: str | None) -> dict:
    """The persisted columns for one signal, from what the engine attached.

    Empty for a signal the engine produced without provenance — nothing is
    filled in by guessing, and such a row stays a legacy row.
    """
    context = signal.context or {}
    timing = context.get("timing") or {}
    prov = context.get("provenance") or {}
    if not timing.get("decision_at") or not prov:
        return {}
    return {
        "bar_open_time": _stamp(timing.get("bar_open_time")),
        "bar_close_time": _stamp(timing.get("bar_close_time")),
        "available_at": _stamp(timing.get("available_at")),
        "received_at": _stamp(timing.get("received_at")),
        "decision_at": _stamp(timing.get("decision_at")),
        "earliest_execution_time": _stamp(timing.get("earliest_execution_time")),
        "clock_basis": PERSISTED,
        "strategy_version": prov.get("strategy_version"),
        "parameter_hash": prov.get("parameter_hash"),
        "input_fingerprint": prov.get("input_fingerprint"),
        "data_source": data_source,
        "code_id": code_id,
        "provenance": prov | {"data_source": data_source, "code_id": code_id,
                              "available_basis": timing.get("available_basis")},
    }
