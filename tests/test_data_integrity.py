"""Repair Pass 2C — data integrity, provenance, market time, option contracts.

One section per finding. Each test states the failure it guards against, and
where a number can be written out by hand it is, because a test that
recomputes the thing under test agrees with every bug it has.
"""
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import decision_provenance, indicators, options, signal_engine, warmup
from app.backtest import measurement
from app.brokers.nse import parse_option_chain
from app.data import clock_grid, contract_specs, importer, readiness, research
from app.models import CandleRecord, CandleRevision, OptionCandle, OptionContract, SignalRecord
from app.optionbuy import contracts as contract_module
from app.optionbuy.chain import (
    CAPTURED,
    LEGACY_BUCKET,
    ChainStore,
    ContractKey,
    ContractMeta,
    OptionBar,
)

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 6, 16)          # a Tuesday session


def ist(day, hh, mm, ss=0):
    return pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST))


def utc(day, hh, mm, ss=0):
    """An IST wall-clock time as a UTC `datetime`, the way the archive stores it."""
    return ist(day, hh, mm, ss).tz_convert("UTC").to_pydatetime()


def full_session(day=DAY, price=24_000.0):
    stamps = [s.tz_convert("UTC") for s in
              pd.date_range(ist(day, 9, 15), ist(day, 15, 25), freq="5min")]
    return pd.DataFrame({"timestamp": stamps, "open": price, "high": price + 5,
                         "low": price - 5, "close": price, "volume": 1000.0})


# ===========================================================================
# TC-1  the exchange clock grid
# ===========================================================================

def test_a_full_session_is_seventy_five_bars_from_0915_to_1525():
    grid = clock_grid.session_grid(DAY, "5m")
    assert len(grid) == 75
    assert grid[0].tz_convert(IST).strftime("%H:%M") == "09:15"
    assert grid[-1].tz_convert(IST).strftime("%H:%M") == "15:25"


def test_a_session_with_the_right_row_count_can_still_be_wrong():
    """The required reproduction: 09:20 missing, 15:30 extra, 75 rows.

    Row counting passed this. The grid reports both faults.
    """
    frame = full_session()
    frame = frame[frame["timestamp"] != ist(DAY, 9, 20).tz_convert("UTC")]
    extra = frame.iloc[[-1]].copy()
    extra["timestamp"] = ist(DAY, 15, 30).tz_convert("UTC")
    frame = pd.concat([frame, extra], ignore_index=True)
    assert len(frame) == 75

    session = clock_grid.validate(frame, "5m").sessions[0]

    assert session.missing == [ist(DAY, 9, 20).isoformat()]
    assert session.out_of_session == [ist(DAY, 15, 30).isoformat()]
    assert set(session.faults()) >= {clock_grid.MISSING,
                                     clock_grid.OUT_OF_SESSION,
                                     clock_grid.INCOMPLETE}
    assert not session.ok


def test_each_fault_is_reported_under_its_own_name():
    frame = full_session()
    off = frame.iloc[[3]].copy()
    off["timestamp"] = ist(DAY, 9, 17).tz_convert("UTC")              # off-grid
    dup = frame.iloc[[10]].copy()                                       # duplicate
    weekend = frame.iloc[[5]].copy()
    weekend["timestamp"] = ist(date(2026, 6, 20), 10, 0).tz_convert("UTC")  # Saturday
    frame = pd.concat([frame, off, dup, weekend], ignore_index=True)

    report = clock_grid.validate(frame, "5m")
    assert report.count(clock_grid.OFF_GRID) == 1
    assert report.count(clock_grid.DUPLICATE) == 1
    assert report.count(clock_grid.OUT_OF_SESSION) == 1     # the Saturday bar
    assert report.count(clock_grid.MISSING) == 0
    assert not report.ok


def test_malformed_bars_are_quarantined_never_snapped():
    """09:17 is not moved to 09:15. Snapping would manufacture a bar the
    exchange never printed, and nothing afterwards would show it."""
    frame = full_session()
    off = frame.iloc[[0]].copy()
    off["timestamp"] = ist(DAY, 9, 17).tz_convert("UTC")
    off["close"] = 99_999.0
    frame = pd.concat([frame, off], ignore_index=True)

    clean, held, _ = clock_grid.quarantine(frame, "5m")

    assert len(clean) == 75
    assert 99_999.0 not in clean["close"].tolist()
    assert held["timestamp"].tolist() == [ist(DAY, 9, 17).tz_convert("UTC")]
    assert clean.attrs["clock_grid"]["policy"] == "quarantine_never_snap"


def test_every_copy_of_a_duplicated_bar_is_quarantined():
    frame = full_session()
    twin = frame.iloc[[10]].copy()
    twin["close"] += 3
    clean, held, _ = clock_grid.quarantine(pd.concat([frame, twin]), "5m")
    assert len(held) == 2 and len(clean) == 74


def test_a_session_still_trading_is_not_missing_its_future():
    frame = full_session().iloc[:10]
    session = clock_grid.validate(frame, "5m", as_of=ist(DAY, 10, 5)).sessions[0]
    assert session.in_progress and session.missing == [] and session.ok


def test_a_wholly_absent_trading_day_is_seventy_five_missing_bars():
    session = clock_grid.validate(full_session().iloc[0:0], "5m",
                                  sessions=[DAY]).sessions[0]
    assert len(session.missing) == 75 and session.incomplete


def test_the_research_loader_quarantines_the_1530_bar(db):
    frame = full_session()
    extra = frame.iloc[[-1]].copy()
    extra["timestamp"] = ist(DAY, 15, 30).tz_convert("UTC")
    rows = pd.concat([frame, extra], ignore_index=True)
    for r in rows.itertuples():
        db.add(CandleRecord(symbol="NIFTY", timeframe="5m",
                            timestamp=r.timestamp.to_pydatetime(), open=r.open,
                            high=r.high, low=r.low, close=r.close, volume=r.volume,
                            source="test", session_date=DAY,
                            ingested_at=datetime(2026, 6, 16, 12, tzinfo=UTC)))
    db.commit()

    loaded = research.load_research_candles(db, "NIFTY", "5m")

    assert len(loaded) == 75
    assert loaded.attrs["clock_grid"]["totals"]["out_of_session"] == 1
    assert loaded.attrs["quarantined"] == [str(ist(DAY, 15, 30).tz_convert("UTC"))]


# ===========================================================================
# TC-2  persistent decision clocks
# ===========================================================================

def test_the_six_clocks_are_ordered_and_checked():
    t = decision_provenance.clocks(
        bar_open=ist(DAY, 10, 0), timeframe_minutes=5,
        received_at=ist(DAY, 10, 5, 2), decision_at=ist(DAY, 10, 5, 3))

    stamps = [pd.Timestamp(t[k]) for k in ("bar_open_time", "bar_close_time",
                                           "available_at", "decision_at",
                                           "earliest_execution_time")]
    assert stamps == sorted(stamps)
    assert pd.Timestamp(t["bar_close_time"]) <= pd.Timestamp(t["available_at"])
    assert pd.Timestamp(t["available_at"]) <= pd.Timestamp(t["decision_at"])
    assert pd.Timestamp(t["earliest_execution_time"]) >= pd.Timestamp(t["decision_at"])
    assert t["received_at"] == ist(DAY, 10, 5, 2).isoformat()
    assert t["available_basis"] == decision_provenance.RECEIVED


def test_received_at_is_never_invented_from_the_exchange_clock():
    """A replay has no receipt to record. It says so rather than borrowing
    the bar's close and calling it a receipt."""
    t = decision_provenance.clocks(bar_open=ist(DAY, 10, 0), timeframe_minutes=5,
                                   received_at=None, decision_at=ist(DAY, 10, 6))
    assert t["received_at"] is None
    assert t["available_basis"] == decision_provenance.BAR_CLOSE_REPLAY
    assert t["available_at"] == ist(DAY, 10, 5).isoformat()


def test_a_decision_that_read_a_bar_before_it_had_it_is_refused():
    with pytest.raises(ValueError, match="could not yet have had"):
        decision_provenance.clocks(bar_open=ist(DAY, 10, 0), timeframe_minutes=5,
                                   received_at=ist(DAY, 10, 7),
                                   decision_at=ist(DAY, 10, 6))


def engine_frame(n=120):
    stamps = pd.date_range(ist(DAY, 9, 15), periods=n, freq="5min").tz_convert("UTC")
    rng = np.random.default_rng(0)
    close = 24_000 + np.cumsum(rng.normal(0, 5, n))
    frame = pd.DataFrame({"timestamp": stamps, "open": close, "high": close + 4,
                          "low": close - 4, "close": close, "volume": 1000.0})
    indicators.declare_volume(frame, indicators.GENUINE)
    return frame


def test_a_live_signal_carries_its_clocks_and_they_persist_as_columns(db):
    frame = engine_frame()
    received = frame["timestamp"].iloc[-1] + pd.Timedelta(minutes=5, seconds=1)
    frame.attrs["received_at"] = received.isoformat()
    frame.attrs["decision_time"] = (received + pd.Timedelta(seconds=1)).isoformat()

    sig = signal_engine.generate(frame)
    columns = decision_provenance.columns(sig, data_source="angel",
                                          code_id="abc123def456")
    db.add(SignalRecord(symbol="NIFTY", timeframe="5m", action=sig.action,
                        confidence=sig.confidence, price=sig.price,
                        checks=[], context=sig.context, **columns))
    db.commit()
    row = db.scalars(select(SignalRecord)).one()

    assert row.clock_basis == "persisted"
    assert pd.Timestamp(row.received_at).tz_localize(None) == received.tz_localize(None)
    assert row.bar_close_time <= row.available_at <= row.decision_at \
        <= row.earliest_execution_time
    assert row.bar_open_time == frame["timestamp"].iloc[-1].to_pydatetime().replace(tzinfo=None) \
        or pd.Timestamp(row.bar_open_time).tz_localize(None) == \
        frame["timestamp"].iloc[-1].tz_localize(None)


def test_a_legacy_row_stays_legacy():
    """Nothing is backfilled: a signal the engine produced without clocks
    yields no clock columns, and its row is left recognisably legacy."""
    legacy = SimpleNamespace(context={"timing": {"signal_time": "2026-06-16T04:40:00+00:00"}})
    assert decision_provenance.columns(legacy, data_source="x", code_id="y") == {}


def test_the_evaluator_reads_the_persisted_clock_before_the_json(db):
    from app.evaluation import outcomes as study

    frame = full_session()
    for r in frame.itertuples():
        db.add(CandleRecord(symbol="NIFTY", timeframe="5m",
                            timestamp=r.timestamp.to_pydatetime(), open=r.open,
                            high=r.high, low=r.low, close=r.close, volume=r.volume,
                            source="test", session_date=DAY,
                            ingested_at=datetime(2026, 6, 16, 12, tzinfo=UTC)))
    bar = ist(DAY, 10, 0).tz_convert("UTC")
    db.add(SignalRecord(
        symbol="NIFTY", timeframe="5m", action="BUY", confidence=0.8,
        price=24_000.0, entry=24_000.0, stop_loss=23_980.0, target=24_040.0,
        checks=[], created_at=(bar + pd.Timedelta(minutes=5, seconds=2)).to_pydatetime(),
        # The JSON says a different bar. The column is the record.
        context={"timing": {"bar_open_time": ist(DAY, 11, 0).isoformat(),
                            "signal_time": ist(DAY, 11, 5, 2).isoformat()}},
        bar_open_time=bar.to_pydatetime(),
        bar_close_time=(bar + pd.Timedelta(minutes=5)).to_pydatetime(),
        available_at=(bar + pd.Timedelta(minutes=5)).to_pydatetime(),
        decision_at=(bar + pd.Timedelta(minutes=5, seconds=2)).to_pydatetime(),
        earliest_execution_time=(bar + pd.Timedelta(minutes=5, seconds=2)).to_pydatetime(),
        clock_basis="persisted"))
    db.commit()

    picked = study.collect(db)
    assert len(picked.outcomes) == 1
    out = picked.outcomes[0]
    assert out.timing_basis == "persisted_clock"
    assert pd.Timestamp(out.signal_bar_time) == bar


# ---- 2C.1: the persisted deadline is authoritative -------------------------

def seed_session(db):
    for r in full_session().itertuples():
        db.add(CandleRecord(symbol="NIFTY", timeframe="5m",
                            timestamp=r.timestamp.to_pydatetime(), open=r.open,
                            high=r.high, low=r.low, close=r.close, volume=r.volume,
                            source="test", session_date=DAY,
                            ingested_at=datetime(2026, 6, 16, 12, tzinfo=UTC)))


def persisted_signal(*, bar_open, bar_close, available, decision, earliest):
    return SignalRecord(
        symbol="NIFTY", timeframe="5m", action="BUY", confidence=0.8,
        price=24_000.0, entry=24_000.0, stop_loss=23_980.0, target=24_040.0,
        checks=[], created_at=utc(DAY, *decision),
        context={"timing": {"bar_open_time": ist(DAY, *bar_open).isoformat(),
                            "signal_time": ist(DAY, *decision).isoformat()}},
        bar_open_time=utc(DAY, *bar_open), bar_close_time=utc(DAY, *bar_close),
        available_at=utc(DAY, *available), decision_at=utc(DAY, *decision),
        earliest_execution_time=utc(DAY, *earliest), clock_basis="persisted")


def test_the_persisted_deadline_is_the_execution_floor(db):
    """decision 10:02, persisted earliest 10:12: the 10:05 bar is refused and
    the first fill is the 10:15 open — the first bar at or after 10:12. The
    default latency alone would have filled at 10:05."""
    from app.evaluation import outcomes as study

    seed_session(db)
    db.add(persisted_signal(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1),
                            decision=(10, 2), earliest=(10, 12)))
    db.commit()

    picked = study.collect(db)
    assert len(picked.outcomes) == 1
    out = picked.outcomes[0]
    assert out.timing_basis == "persisted_clock"
    assert pd.Timestamp(out.earliest_execution_time) == ist(DAY, 10, 12)
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 15)


def test_a_1005_fill_under_a_1012_deadline_is_refused_before_the_price_is_read(db):
    from app.backtest.costs import FlatCostModel, SlippageModel
    from app.backtest.feed import HistoricalFeed
    from app.evaluation import outcomes as study

    feed = HistoricalFeed(full_session())
    stamps = feed.stamps()
    signal_index = int(stamps.searchsorted(ist(DAY, 9, 55)))
    candidate = int(stamps.searchsorted(ist(DAY, 10, 5)))
    record = persisted_signal(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1),
                              decision=(10, 2), earliest=(10, 12))

    reads = []
    original = feed.execution_open
    feed.execution_open = lambda *a: reads.append(a) or original(*a)
    with pytest.raises(ValueError, match="precedes the earliest"):
        study.evaluate_signal(feed, signal_index, record, FlatCostModel(per_round_trip=0),
                              SlippageModel(), execution_index=candidate,
                              timing_basis="persisted_clock",
                              decision_time=ist(DAY, 10, 2),
                              execution_floor=ist(DAY, 10, 12))
    assert reads == []

    # Left to find its own bar, the evaluator goes to the floor, not to the
    # decision plus the default latency.
    out = study.evaluate_signal(feed, signal_index, record, FlatCostModel(per_round_trip=0),
                                SlippageModel(), decision_time=ist(DAY, 10, 2),
                                execution_floor=ist(DAY, 10, 12))
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 15)


# ---- 2C.2: direct evaluate_signal() enforces the persisted clocks itself ---

def direct(record, *, candidate=None, floor=None, policy=None,
           basis="recorded_bar_time"):
    """Call evaluate_signal directly — no collect() — counting price reads."""
    from app.backtest.costs import FlatCostModel, SlippageModel
    from app.backtest.feed import HistoricalFeed
    from app.evaluation import outcomes as study

    feed = HistoricalFeed(full_session())
    stamps = feed.stamps()
    reads = []
    original = feed.execution_open
    feed.execution_open = lambda *a: reads.append(a) or original(*a)
    signal_index = int(stamps.searchsorted(ist(DAY, 9, 55)))
    kwargs = {}
    if candidate is not None:
        kwargs["execution_index"] = int(stamps.searchsorted(ist(DAY, *candidate)))
    if floor is not None:
        kwargs["execution_floor"] = ist(DAY, *floor)
    if policy is not None:
        kwargs["policy"] = policy
    try:
        out = study.evaluate_signal(feed, signal_index, record,
                                    FlatCostModel(per_round_trip=0), SlippageModel(),
                                    timing_basis=basis, **kwargs)
    except ValueError as refused:                   # ClockViolation included
        return None, refused, len(reads)
    return out, None, len(reads)


DEADLINE_1012 = ist(DAY, 10, 12).tz_convert("UTC").isoformat()   # as the error prints it


def valid_1012():
    return persisted_signal(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1),
                            decision=(10, 2), earliest=(10, 12))


def test_direct_call_without_a_floor_still_refuses_the_1005_bar():
    """Case A — Codex's reproduction: no execution_floor passed."""
    out, refused, reads = direct(valid_1012(), candidate=(10, 5))
    assert out is None and "precedes the earliest" in str(refused)
    assert DEADLINE_1012 in str(refused)
    assert reads == 0


def test_direct_call_with_a_weaker_caller_floor_is_still_refused():
    """Case B — a caller floor of 10:05 cannot loosen the persisted 10:12."""
    out, refused, reads = direct(valid_1012(), candidate=(10, 5), floor=(10, 5))
    assert out is None and DEADLINE_1012 in str(refused)
    assert reads == 0


def test_direct_call_with_a_stronger_caller_floor_tightens_it():
    """Case C — a caller floor of 10:20 is later than 10:12 and wins."""
    out, _, reads = direct(valid_1012(), floor=(10, 20))
    assert pd.Timestamp(out.earliest_execution_time) == ist(DAY, 10, 20)
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 20)
    refused = direct(valid_1012(), candidate=(10, 15), floor=(10, 20))
    assert refused[0] is None and refused[2] == 0


def test_direct_call_with_a_stricter_policy_latency_uses_it():
    """Case D — decision 10:02 + 15 min latency = 10:17, later than 10:12."""
    from app.backtest.execution import ExecutionPolicy

    out, _, _ = direct(valid_1012(), policy=ExecutionPolicy(latency_seconds=900))
    assert pd.Timestamp(out.earliest_execution_time) == ist(DAY, 10, 17)
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 20)
    refused = direct(valid_1012(), candidate=(10, 15),
                     policy=ExecutionPolicy(latency_seconds=900))
    assert refused[0] is None and refused[2] == 0


def test_direct_call_at_exactly_the_persisted_deadline_is_eligible():
    """Case E — a bar opening exactly at the persisted deadline may fill."""
    row = persisted_signal(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1),
                           decision=(10, 2), earliest=(10, 10))
    out, refused, reads = direct(row, candidate=(10, 10))
    assert refused is None
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 10)
    assert out.timing_basis == "persisted_clock"
    assert reads == 1


@pytest.mark.parametrize("clocks,missing", [
    (dict(bar_open=(9, 55), bar_close=(10, 0), available=(9, 59), decision=(10, 2),
          earliest=(10, 12)), None),
    (dict(bar_open=(9, 55), bar_close=(10, 0), available=(10, 3), decision=(10, 2),
          earliest=(10, 12)), None),
    (dict(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1), decision=(10, 2),
          earliest=(10, 1)), None),
    (dict(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1), decision=(10, 2),
          earliest=(10, 12)), "earliest_execution_time"),
    (dict(bar_open=(9, 55), bar_close=(10, 5), available=(10, 5), decision=(10, 6),
          earliest=(10, 12)), None),
], ids=["available_before_close", "decision_before_available",
        "earliest_before_decision", "missing_earliest", "wrong_bar_interval"])
def test_direct_call_with_malformed_persisted_clocks_fails_closed(clocks, missing):
    """Case F — refused before any price access, whatever bar is offered."""
    from app.analytics.decision_provenance import ClockViolation

    row = persisted_signal(**clocks)
    if missing:
        setattr(row, missing, None)
    for candidate in ((10, 15), None):
        out, refused, reads = direct(row, candidate=candidate)
        assert out is None and isinstance(refused, ClockViolation)
        assert reads == 0


def test_direct_call_on_a_legacy_row_is_labelled_legacy_and_never_persisted():
    """Case G — no persisted clocks: the labelled fallback, and a claim of
    persisted_clock is refused rather than printed."""
    from app.analytics.decision_provenance import ClockViolation

    row = valid_1012()
    for name in ("bar_open_time", "bar_close_time", "available_at", "decision_at",
                 "earliest_execution_time", "clock_basis"):
        setattr(row, name, None)
    out, _, _ = direct(row, candidate=(10, 5))
    assert out.timing_basis == "recorded_bar_time"
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 5)

    out, refused, reads = direct(row, candidate=(10, 5), basis="persisted_clock")
    assert out is None and isinstance(refused, ClockViolation) and reads == 0


def test_a_row_loaded_without_its_provenance_group_is_still_enforced(db):
    """A plain select leaves the deferred clocks unloaded. That does not make
    the row legacy: the clocks are read and enforced."""
    db.add(valid_1012())
    db.commit()
    db.expunge_all()
    row = db.scalars(select(SignalRecord)).one()
    out, refused, reads = direct(row, candidate=(10, 5))
    assert out is None and DEADLINE_1012 in str(refused) and reads == 0


@pytest.mark.parametrize("clocks", [
    # available_at < bar_close
    dict(bar_open=(9, 55), bar_close=(10, 0), available=(9, 59), decision=(10, 2),
         earliest=(10, 12)),
    # decision_at < available_at
    dict(bar_open=(9, 55), bar_close=(10, 0), available=(10, 3), decision=(10, 2),
         earliest=(10, 12)),
    # earliest_execution_time < decision_at
    dict(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1), decision=(10, 2),
         earliest=(10, 1)),
], ids=["available_before_close", "decision_before_available",
        "earliest_before_decision"])
def test_malformed_persisted_clocks_fail_closed(db, clocks):
    """Refused outright — not re-read through the legacy JSON, which would
    have produced a valid-looking fill under a weaker clock."""
    from app.evaluation import outcomes as study

    seed_session(db)
    db.add(persisted_signal(**clocks))
    db.commit()

    picked = study.collect(db)
    assert picked.outcomes == []
    assert picked.report.invalid_timing == 1
    assert picked.report.legacy_inferred_timing == 0


def test_a_persisted_row_missing_its_deadline_fails_closed(db):
    from app.evaluation import outcomes as study

    seed_session(db)
    row = persisted_signal(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1),
                           decision=(10, 2), earliest=(10, 12))
    row.earliest_execution_time = None
    db.add(row)
    db.commit()

    picked = study.collect(db)
    assert picked.outcomes == []
    assert picked.report.invalid_timing == 1


def test_a_legacy_row_is_never_labelled_persisted_clock(db):
    from app.evaluation import outcomes as study

    seed_session(db)
    row = persisted_signal(bar_open=(9, 55), bar_close=(10, 0), available=(10, 1),
                           decision=(10, 2), earliest=(10, 12))
    for name in ("bar_open_time", "bar_close_time", "available_at", "decision_at",
                 "earliest_execution_time", "clock_basis"):
        setattr(row, name, None)
    db.add(row)
    db.commit()

    out = study.collect(db).outcomes[0]
    assert out.timing_basis == "recorded_bar_time"
    # The legacy fallback: decision plus the policy latency, not the 10:12
    # that only a persisted row could have carried.
    assert pd.Timestamp(out.actual_fill_time) == ist(DAY, 10, 5)


# ===========================================================================
# RP-2  per-signal provenance
# ===========================================================================

def test_the_parameter_hash_is_deterministic_and_moves_with_a_parameter(monkeypatch):
    a = decision_provenance.parameter_hash(signal_engine.parameters("5m", 2.0, 1.2))
    b = decision_provenance.parameter_hash(signal_engine.parameters("5m", 2.0, 1.2))
    assert a == b and len(a) == 64
    monkeypatch.setitem(signal_engine.WEIGHTS, "vwap", 0.17)
    assert decision_provenance.parameter_hash(
        signal_engine.parameters("5m", 2.0, 1.2)) != a


def test_the_input_fingerprint_moves_with_one_price_and_tells_nan_from_zero():
    frame = engine_frame(40)
    base = decision_provenance.input_fingerprint(frame, None, 14.2)
    assert base == decision_provenance.input_fingerprint(frame.copy(), None, 14.2)

    nudged = frame.copy()
    nudged.loc[20, "close"] += 0.05
    assert decision_provenance.input_fingerprint(nudged, None, 14.2) != base

    chain = pd.DataFrame({"strike": [24_000.0], "call_oi": [np.nan], "put_oi": [10.0]})
    zeroed = chain.assign(call_oi=[0.0])
    assert decision_provenance.input_fingerprint(frame, chain, None) != \
        decision_provenance.input_fingerprint(frame, zeroed, None)


def test_one_stored_signal_says_what_produced_it(db):
    frame = engine_frame()
    frame.attrs["decision_time"] = (frame["timestamp"].iloc[-1]
                                    + pd.Timedelta(minutes=6)).isoformat()
    sig = signal_engine.generate(frame, india_vix=13.0)
    db.add(SignalRecord(symbol="NIFTY", timeframe="5m", action=sig.action,
                        confidence=sig.confidence, price=sig.price, checks=[],
                        context=sig.context,
                        **decision_provenance.columns(
                            sig, data_source="angel", code_id="01eea396df56")))
    db.commit()
    row = db.scalars(select(SignalRecord)).one()

    assert row.strategy_version == decision_provenance.STRATEGY_VERSION
    assert row.parameter_hash == decision_provenance.parameter_hash(
        signal_engine.parameters("5m", 2.0, 1.2))
    assert row.input_fingerprint == decision_provenance.input_fingerprint(
        indicators.drop_unclosed(frame, "5m", as_of=pd.Timestamp(
            frame.attrs["decision_time"])), None, 13.0)
    assert row.data_source == "angel" and row.code_id == "01eea396df56"
    assert row.provenance["parameters"]["min_confidence"] == signal_engine.MIN_CONFIDENCE


# ===========================================================================
# RP-4  git / dirty-worktree provenance
# ===========================================================================

@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-qm", "init")
    return tmp_path


def test_a_clean_tree_is_identified_by_its_commit(repo):
    state = measurement.git_state(repo)
    assert len(state["git_commit"]) == 40
    assert state["dirty_worktree"] is False
    assert state["dirty_diff_sha256"] is None
    assert measurement.code_id(state) == state["git_commit"][:12]


def test_a_dirty_tree_is_never_labelled_with_the_commit_alone(repo):
    (repo / "a.py").write_text("x = 2\n")
    first = measurement.git_state(repo)
    assert first["dirty_worktree"] is True
    assert len(first["dirty_diff_sha256"]) == 64
    assert "+dirty." in measurement.code_id(first)

    # Deterministic for the same edit, different for a further one —
    # including one that only adds an untracked file.
    assert measurement.git_state(repo)["dirty_diff_sha256"] == first["dirty_diff_sha256"]
    (repo / "b.py").write_text("y = 1\n")
    assert measurement.git_state(repo)["dirty_diff_sha256"] != first["dirty_diff_sha256"]


def test_no_git_is_said_rather_than_guessed(tmp_path):
    state = measurement.git_state(tmp_path)
    assert state["git_commit"] == measurement.GIT_UNAVAILABLE
    assert state["dirty_worktree"] is None


def test_backtest_provenance_carries_the_git_state():
    block = measurement.provenance(pd.DataFrame({"a": [1]}), {"k": 1})
    assert set(block["git"]) >= {"git_commit", "dirty_worktree", "dirty_diff_sha256"}


# ===========================================================================
# TC-5  declared warmup
# ===========================================================================

def closes(n):
    stamps = pd.date_range("2026-06-01 03:45", periods=n, freq="5min", tz="UTC")
    c = 24_000 + np.arange(n, dtype=float)
    return pd.DataFrame({"timestamp": stamps, "open": c, "high": c + 1,
                         "low": c - 1, "close": c, "volume": 1.0})


def test_insufficient_history_leaves_the_feature_unavailable():
    enriched = indicators.enrich(closes(199))
    assert enriched["ema200"].isna().all()
    # EMA50 is valid from its 50th bar — row 49 — and blank before it.
    assert enriched["ema50"].iloc[49:].notna().all()
    assert enriched["ema50"].iloc[:49].isna().all()


def test_exactly_enough_history_gives_the_first_valid_point():
    enriched = indicators.enrich(closes(200))
    assert enriched["ema200"].iloc[:199].isna().all()
    assert not np.isnan(enriched["ema200"].iloc[199])


def test_live_and_replay_with_the_same_declared_history_agree():
    """The live path trims to the declared history; the backtest feed
    windows to the same length. Same bars in, same indicators out."""
    from app.backtest.feed import HistoricalFeed

    frame = closes(500)
    live = indicators.enrich(warmup.declared_history(frame))
    feed = HistoricalFeed(frame)
    feed.seek(499)
    replay = indicators.enrich(feed.view(499))

    assert len(live) == len(replay) == warmup.ANALYSIS_HISTORY_BARS
    pd.testing.assert_frame_equal(live.reset_index(drop=True),
                                  replay.reset_index(drop=True), check_like=True)


def test_a_trend_check_before_warmup_is_unavailable_not_neutral():
    row = indicators.enrich(closes(40)).iloc[-1]
    check = signal_engine.check_trend(row)
    assert check.disabled
    assert "warmup" in check.reason


def test_the_history_covers_the_longest_warmup():
    assert warmup.ANALYSIS_HISTORY_BARS >= max(warmup.WARMUP_BARS.values())


# ===========================================================================
# TC-6  as-known revisions
# ===========================================================================

def frozen(moment):
    """A `datetime` whose `now()` is `moment`, in UTC.

    A subclass rather than a stand-in object, so the importer's own
    `isinstance(..., datetime)` checks still hold. UTC because that is what
    the importer writes: SQLite stores an aware value without converting it,
    so an IST stamp would come back five and a half hours off.
    """
    utc = pd.Timestamp(moment).tz_convert("UTC").to_pydatetime()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc if tz is None else utc.astimezone(tz)
    return Frozen


def bar_0955(close):
    ts = ist(DAY, 9, 55).tz_convert("UTC")
    return pd.DataFrame([{"timestamp": ts, "open": 99.0, "high": 102.0,
                          "low": 98.0, "close": close, "volume": 10.0}])


def test_a_replay_sees_the_bar_as_it_was_known_at_the_decision(db, monkeypatch):
    """09:55 close first 100, signal at 10:02, source restates 101 at 10:07.

    The 10:02 replay sees 100. The latest series sees 101 and names it a
    revision. The 100 is not destroyed.
    """
    monkeypatch.setattr(importer, "datetime", frozen(ist(DAY, 10, 0)))
    importer.import_index_candles(db, bar_0955(100.0), "NIFTY", "5m", "test")

    monkeypatch.setattr(importer, "datetime", frozen(ist(DAY, 10, 7)))
    second = importer.import_index_candles(db, bar_0955(101.0), "NIFTY", "5m", "test")
    assert second.revised == 1

    replay, basis = research.as_known(db, "NIFTY", "5m", ist(DAY, 10, 2))
    assert replay["close"].tolist() == [100.0]
    assert basis["earlier_revision"] == 1

    latest = research.load_research_candles(db, "NIFTY", "5m")
    assert latest["close"].tolist() == [101.0]
    assert latest.attrs["revisions"]["revised_bars"] == 1
    assert latest.attrs["revisions"]["revised_with_history"] == 1

    kept = db.scalars(select(CandleRevision)).one()
    assert (kept.close, kept.revision) == (100.0, 0)
    assert db.scalars(select(CandleRecord)).one().revision == 1


def test_a_bar_first_stored_after_the_decision_does_not_exist_for_it(db, monkeypatch):
    monkeypatch.setattr(importer, "datetime", frozen(ist(DAY, 10, 30)))
    importer.import_index_candles(db, bar_0955(100.0), "NIFTY", "5m", "test")
    replay, basis = research.as_known(db, "NIFTY", "5m", ist(DAY, 10, 2))
    assert replay.empty and basis["not_yet_known"] == 1


def test_an_identical_reimport_is_not_a_revision(db):
    importer.import_index_candles(db, bar_0955(100.0), "NIFTY", "5m", "test")
    again = importer.import_index_candles(db, bar_0955(100.0), "NIFTY", "5m", "test")
    assert (again.revised, again.unchanged) == (0, 1)
    assert db.scalars(select(CandleRecord)).one().revision == 0
    assert db.scalars(select(CandleRevision)).all() == []


def test_a_legacy_restatement_is_reported_unrecoverable_not_served(db):
    """Rows restated before revisions were archived have lost their earlier
    values. An as-known read leaves them out and counts them."""
    db.add(CandleRecord(symbol="NIFTY", timeframe="5m",
                        timestamp=ist(DAY, 9, 55).tz_convert("UTC").to_pydatetime(),
                        open=1, high=2, low=0.5, close=1.5, volume=1, source="test",
                        session_date=DAY, revision=7,
                        ingested_at=ist(DAY, 15, 0).to_pydatetime()))
    db.commit()
    replay, basis = research.as_known(db, "NIFTY", "5m", ist(DAY, 10, 2))
    assert replay.empty and basis["unrecoverable"] == 1


# ===========================================================================
# OC-1  exchange time vs capture time
# ===========================================================================

KEY = ContractKey(expiry=date(2026, 6, 23), strike=24_000.0, option_type="CE")


def a_bar(*, bucket, exchange=None, captured=None):
    return OptionBar(row_id=1, contract_id=1, key=KEY,
                     timestamp=bucket.to_pydatetime(), open=100.0, high=100.0,
                     low=100.0, close=100.0, volume=1.0, open_interest=1.0,
                     iv=0.15, bid=None, ask=None, underlying_close=24_000.0,
                     bar_kind="snapshot", source="test", samples=1,
                     session_date=DAY,
                     exchange_time=exchange.to_pydatetime() if exchange is not None else None,
                     capture_time=captured.to_pydatetime() if captured is not None else None,
                     first_seen=captured.to_pydatetime() if captured is not None else None)


def a_store(bar):
    meta = ContractMeta(contract_id=1, key=KEY, lot_size=None, tradingsymbol=None,
                        source="test", first_seen=datetime(2026, 1, 1, tzinfo=UTC))
    return ChainStore({KEY: [bar]}, {KEY: meta})


def test_a_quote_captured_at_1004_is_unusable_at_1002():
    """exchange 10:00, captured 10:04, decision 10:02 → unusable. The bucket
    start used to be enough to make it look available."""
    store = a_store(a_bar(bucket=ist(DAY, 10, 0), exchange=ist(DAY, 10, 0),
                          captured=ist(DAY, 10, 4)))
    store.seek(ist(DAY, 10, 2).to_pydatetime())
    assert store.bar_at(KEY, ist(DAY, 10, 2).to_pydatetime()) is None

    store.seek(ist(DAY, 10, 6).to_pydatetime())
    found = store.bar_at(KEY, ist(DAY, 10, 6).to_pydatetime())
    assert found is not None
    assert found.timestamp_basis == CAPTURED
    assert store.available_from(found) == ist(DAY, 10, 4)


def test_age_is_measured_from_the_quote_not_from_its_bucket():
    """exchange 10:03 in the 10:00 bucket, read at 10:12 with a 10-minute
    staleness limit: 9 minutes old by the quote, 12 by the bucket. Fresh."""
    store = a_store(a_bar(bucket=ist(DAY, 10, 0), exchange=ist(DAY, 10, 3),
                          captured=ist(DAY, 10, 3, 5)))
    store.seek(ist(DAY, 10, 12).to_pydatetime())
    assert store.bar_at(KEY, ist(DAY, 10, 12).to_pydatetime()) is not None


def a_store_with(bar, staleness_minutes):
    meta = ContractMeta(contract_id=1, key=KEY, lot_size=None, tradingsymbol=None,
                        source="test", first_seen=datetime(2026, 1, 1, tzinfo=UTC))
    return ChainStore({KEY: [bar]}, {KEY: meta}, staleness_minutes=staleness_minutes)


def test_a_late_quote_is_newly_available_and_already_old():
    """exchange 10:00, bucket 10:05, captured 10:09, evaluated 10:09.
    Available only from 10:09; nine minutes old at 10:09. The report used to
    say four — measured from the bucket — while the guard measured nine."""
    from app.optionbuy import pricing

    late = a_bar(bucket=ist(DAY, 10, 5), exchange=ist(DAY, 10, 0),
                 captured=ist(DAY, 10, 9))
    store = a_store_with(late, staleness_minutes=10)

    store.seek(ist(DAY, 10, 8, 59).to_pydatetime())
    assert store.bar_at(KEY, ist(DAY, 10, 8, 59).to_pydatetime()) is None

    now = ist(DAY, 10, 9).to_pydatetime()
    store.seek(now)
    seen = store.bar_at(KEY, now)
    assert seen is not None
    assert store.available_from(seen) == ist(DAY, 10, 9)
    assert seen.age(now) == timedelta(minutes=9)
    assert seen.age_basis == "exchange_time"

    quote = pricing._from_bar(seen, now)                      # noqa: SLF001
    assert "9 min old" in quote.basis
    assert "4 min old" not in quote.basis
    assert quote.reference["observation_age_seconds"] == 540.0
    assert quote.reference["age_basis"] == "exchange_time"
    assert pd.Timestamp(quote.reference["observed_at"]) == ist(DAY, 10, 0)


def test_a_newly_received_quote_can_already_be_stale():
    """Same quote, five-minute staleness limit: it arrives at 10:09 and is
    refused at 10:09, because the market it describes is nine minutes old."""
    late = a_bar(bucket=ist(DAY, 10, 5), exchange=ist(DAY, 10, 0),
                 captured=ist(DAY, 10, 9))
    store = a_store_with(late, staleness_minutes=5)
    now = ist(DAY, 10, 9).to_pydatetime()
    store.seek(now)
    assert store.bar_at(KEY, now) is None
    assert store.bar_at(KEY, now, allow_stale=True) is late


def test_without_an_exchange_stamp_age_falls_back_to_capture_and_says_so():
    from app.optionbuy import pricing

    unstamped = a_bar(bucket=ist(DAY, 10, 5), captured=ist(DAY, 10, 7))
    now = ist(DAY, 10, 9).to_pydatetime()
    assert unstamped.age_basis == "capture_time_no_exchange_stamp"
    assert unstamped.age(now) == timedelta(minutes=2)
    assert pricing._from_bar(unstamped, now).reference["age_basis"] \
        == "capture_time_no_exchange_stamp"                    # noqa: SLF001

    legacy = a_bar(bucket=ist(DAY, 10, 5))
    assert legacy.age_basis == "legacy_bucket_start"
    assert legacy.age(now) == timedelta(minutes=4)


def test_a_legacy_row_is_read_under_the_labelled_bucket_rule():
    bar = a_bar(bucket=ist(DAY, 10, 0))
    store = a_store(bar)
    assert bar.timestamp_basis == LEGACY_BUCKET
    assert store.available_from(bar) == ist(DAY, 10, 5)


def nse_payload(**ce_extra):
    ce = {"openInterest": 1200, "changeinOpenInterest": 10,
          "totalTradedVolume": 500, "impliedVolatility": 14.0,
          "lastPrice": 101.5, **ce_extra}
    pe = {"openInterest": 900, "changeinOpenInterest": -5,
          "totalTradedVolume": 300, "impliedVolatility": 15.0, "lastPrice": 88.0}
    return {"records": {"underlyingValue": 24_010.0, "expiryDates": ["23-Jun-2026"],
                        "data": [{"strikePrice": 24_000, "expiryDate": "23-Jun-2026",
                                  "CE": ce, "PE": pe}]}}


def import_poll(db, chain, captured, monkeypatch):
    monkeypatch.setattr(importer, "datetime",
                        frozen(pd.Timestamp(captured) + pd.Timedelta(seconds=1)))
    return importer.import_option_snapshot(
        db, chain, underlying="NIFTY", expiry="23-Jun-2026", spot=24_010.0,
        source="nse", captured_at=captured, timeframe="5m")


def test_first_seen_is_immutable_and_capture_time_moves(db, monkeypatch):
    """Checked after every poll, not only at the end — a store that rewrote
    `first_seen` on each poll would otherwise pass by ending on the first."""
    def ce():
        db.expire_all()
        return db.scalars(select(OptionCandle).join(OptionContract).where(
            OptionContract.option_type == "CE")).one()

    def naive(value):
        return pd.Timestamp(value).tz_localize(None) if pd.Timestamp(value).tzinfo is None \
            else pd.Timestamp(value).tz_convert("UTC").tz_localize(None)

    first, later = utc(DAY, 10, 1), utc(DAY, 10, 3)
    parsed, _ = parse_option_chain(nse_payload())
    import_poll(db, parsed, first, monkeypatch)
    assert naive(ce().first_seen) == naive(first) == naive(ce().capture_time)

    moved, _ = parse_option_chain(nse_payload(lastPrice=104.0))
    import_poll(db, moved, later, monkeypatch)
    row = ce()
    assert naive(row.first_seen) == naive(first)        # never moves
    assert naive(row.capture_time) == naive(later)      # the newest poll
    assert row.close == 104.0

    # Re-importing the older poll must not rewind the bar.
    import_poll(db, parsed, first, monkeypatch)
    row = ce()
    assert naive(row.first_seen) == naive(first)
    assert naive(row.capture_time) == naive(later)
    assert row.close == 104.0


# ===========================================================================
# OC-4  dated expiries and lot sizes
# ===========================================================================

def meta(lot, source):
    return SimpleNamespace(lot_size=lot, source=source, contract_id=7,
                           tradingsymbol="NIFTY23JUN2624000CE")


def evidence(lot, *, source="NSE", reference="NSE/FAOP/TEST/1",
             effective_from=date(2026, 1, 1), effective_to=None):
    return contract_specs.LotSizeEvidence(lot_size=lot, effective_from=effective_from,
                                          source=source, reference=reference,
                                          effective_to=effective_to)


def spec_for(meta_, *, on=date(2026, 6, 16), schedule=()):
    return contract_specs.resolve(underlying="NIFTY", key=KEY, meta=meta_, on=on,
                                  schedule=schedule,
                                  expiry_basis=contract_specs.ARCHIVE_LISTED)


@pytest.mark.parametrize("lot,source", [(75, "free"), (75, "angel"), (65, "kite")])
def test_a_recorded_lot_without_evidence_is_unverified_whatever_the_source(lot, source):
    """A broker's name on the row is who served the quote, not proof of who
    set the lot. The archive's angel/kite rows carry the configured default."""
    spec = spec_for(meta(lot, source))
    assert (spec.lot_size, spec.lot_size_basis, spec.lot_size_verified) \
        == (lot, contract_specs.CONFIGURED_UNVERIFIED, False)


def test_a_source_published_lot_with_auditable_evidence_is_verified():
    observed = SimpleNamespace(**vars(meta(65, "angel")),
                               lot_size_evidence=evidence(
                                   65, source="Angel One instrument master",
                                   reference="OpenAPIScripMaster.json@2026-06-16"))
    spec = spec_for(observed)
    assert (spec.lot_size, spec.lot_size_basis, spec.lot_size_verified) \
        == (65, contract_specs.SOURCE_PUBLISHED, True)
    assert spec.citation == "OpenAPIScripMaster.json@2026-06-16"


@pytest.mark.parametrize("broken", [
    dict(reference=None), dict(source=" "), dict(effective_from=None),
    dict(effective_from=date(2026, 7, 1)),          # not yet in force
    dict(effective_to=date(2026, 6, 1)),            # no longer in force
], ids=["no_reference", "no_source", "no_date", "future", "expired"])
def test_incomplete_or_out_of_date_evidence_never_verifies(broken):
    observed = SimpleNamespace(**vars(meta(65, "angel")),
                               lot_size_evidence=evidence(65, **broken))
    spec = spec_for(observed)
    assert spec.lot_size_verified is False
    assert spec.lot_size_basis == contract_specs.CONFIGURED_UNVERIFIED


def test_evidence_for_a_different_lot_does_not_verify_the_recorded_one():
    observed = SimpleNamespace(**vars(meta(75, "angel")), lot_size_evidence=evidence(65))
    assert spec_for(observed).lot_size_verified is False


def test_a_cited_schedule_is_used_by_date_and_verified_only_with_its_citation():
    schedule = (
        contract_specs.LotSizeEntry("NIFTY", date(2026, 1, 1), 70, "NSE/FAOP/TEST/1"),
        contract_specs.LotSizeEntry("NIFTY", date(2026, 7, 1), 60, "NSE/FAOP/TEST/2"),
    )
    june = spec_for(meta(75, "free"), schedule=schedule)
    july = spec_for(None, on=date(2026, 7, 2), schedule=schedule)
    assert (june.lot_size, june.citation, june.lot_size_verified) \
        == (70, "NSE/FAOP/TEST/1", True)
    assert (july.lot_size, july.lot_size_basis, july.lot_size_verified) \
        == (60, contract_specs.CITED_SCHEDULE, True)


@pytest.mark.parametrize("citation", [None, "", "  "])
def test_a_schedule_entry_without_a_citation_is_unverified(citation):
    schedule = (contract_specs.LotSizeEntry("NIFTY", date(2026, 1, 1), 70, citation),)
    spec = spec_for(None, schedule=schedule)
    assert (spec.lot_size, spec.lot_size_basis, spec.lot_size_verified) \
        == (70, contract_specs.CITED_SCHEDULE_UNVERIFIED, False)
    assert spec.citation is None


def test_a_hand_built_cited_spec_without_evidence_is_not_verified():
    """The escalation Codex found: the label alone used to verify it."""
    spec = contract_specs.ContractSpec(
        underlying="NIFTY", strike=24_000.0, option_type="CE",
        expiry=date(2026, 6, 23), expiry_basis=contract_specs.ARCHIVE_LISTED,
        contract_id=None, tradingsymbol=None, lot_size=75,
        lot_size_basis=contract_specs.CITED_SCHEDULE, citation=None)
    assert spec.lot_size_verified is False
    assert spec.to_dict()["lot_size_verified"] is False


def test_no_lot_size_is_invented_by_default():
    assert contract_specs.LOT_SIZE_SCHEDULE == ()


def test_a_holiday_shifted_expiry_is_taken_from_the_listing_not_a_weekday():
    """The archive lists a Monday expiry — the Tuesday was a holiday. The
    listed contract is chosen; a weekday calendar would have invented one."""
    monday = date(2026, 6, 22)
    key = ContractKey(expiry=monday, strike=24_000.0, option_type="CE")
    bar = OptionBar(row_id=1, contract_id=1, key=key,
                    timestamp=ist(DAY, 9, 15).to_pydatetime(), open=100.0,
                    high=100.0, low=100.0, close=100.0, volume=1.0,
                    open_interest=1.0, iv=0.15, bid=None, ask=None,
                    underlying_close=24_000.0, bar_kind="snapshot", source="t",
                    samples=1, session_date=DAY)
    store = ChainStore({key: [bar]}, {key: ContractMeta(
        contract_id=1, key=key, lot_size=None, tradingsymbol=None, source="t",
        first_seen=datetime(2026, 1, 1, tzinfo=UTC))})
    store.seek(ist(DAY, 11, 0).to_pydatetime())
    config = contract_module.SelectionConfig(expiry_weekday=1, min_days_to_expiry=0.5)

    listed, _ = contract_module.choose_expiry(ist(DAY, 11, 0).to_pydatetime(), store,
                                              config, use_archive=True)
    synthetic, _ = contract_module.choose_expiry(ist(DAY, 11, 0).to_pydatetime(), store,
                                                 config, use_archive=False)
    assert listed == monday
    assert synthetic == date(2026, 6, 23)          # the weekday guess, labelled below


# ===========================================================================
# OC-5  missing OI is unavailable, not zero
# ===========================================================================

def chain(call_oi, put_oi, strikes=(23_900.0, 24_000.0, 24_100.0)):
    return pd.DataFrame({"strike": list(strikes), "call_oi": call_oi,
                         "put_oi": put_oi})


NAN = float("nan")
STRIKES5 = (23_800.0, 23_900.0, 24_000.0, 24_100.0, 24_200.0)


@pytest.mark.parametrize("call_oi,put_oi,resistance,support", [
    # all call OI missing: no resistance, puts still rank
    ([NAN] * 5, [10.0, 20.0, 30.0, 5.0, 1.0], [], [24_000.0, 23_900.0, 23_800.0]),
    # all put OI missing: no support
    ([1.0, 2.0, 30.0, 20.0, 10.0], [NAN] * 5, [24_000.0, 24_100.0, 24_200.0], []),
    # both missing: no levels at all
    ([NAN] * 5, [NAN] * 5, [], []),
    # one valid strike each side, the rest missing: exactly that one
    ([NAN, NAN, NAN, 40.0, NAN], [NAN, 50.0, NAN, NAN, NAN], [24_100.0], [23_900.0]),
    # fewer valid rows than top-N: only the valid ones, never padded
    ([NAN, NAN, 5.0, NAN, 9.0], [7.0, NAN, 3.0, NAN, NAN], [24_000.0, 24_200.0],
     [24_000.0, 23_800.0]),
], ids=["calls_missing", "puts_missing", "both_missing", "one_valid",
        "fewer_than_top"])
def test_oi_levels_never_return_a_strike_with_missing_oi(call_oi, put_oi,
                                                         resistance, support):
    """`nlargest` pads with NaN rows when asked for more than hold a number;
    a chain with no OI used to come back with walls."""
    got_resistance, got_support = options.oi_levels(
        chain(call_oi, put_oi, STRIKES5), spot=24_000.0, top=3)
    assert got_resistance == resistance
    assert got_support == support


def test_a_genuine_zero_oi_strike_is_an_observation_and_can_rank():
    got_resistance, got_support = options.oi_levels(
        chain([NAN, NAN, 0.0, NAN, NAN], [NAN, NAN, 0.0, NAN, NAN], STRIKES5),
        spot=24_000.0, top=3)
    assert (got_resistance, got_support) == ([24_000.0], [24_000.0])


def test_a_chain_summary_with_no_oi_names_no_walls():
    summary = options.summarise(chain([NAN] * 5, [NAN] * 5, STRIKES5), spot=24_000.0)
    assert summary.resistance_strikes == [] and summary.support_strikes == []
    assert summary.oi_status == options.OI_UNAVAILABLE


def test_all_call_oi_missing_is_unavailable_not_bearish():
    s = options.summarise(chain([np.nan] * 3, [100.0, 200.0, 300.0]), 24_000.0)
    assert s.oi_status == options.OI_UNAVAILABLE
    assert s.pcr_oi is None and s.pcr_status == "missing"
    assert s.max_pain is None
    assert s.bias == options.BIAS_UNAVAILABLE
    check = signal_engine.check_option_chain(s)
    assert check.disabled and "unavailable" in check.reason


def test_all_put_oi_missing_is_unavailable():
    s = options.summarise(chain([100.0, 200.0, 300.0], [np.nan] * 3), 24_000.0)
    assert s.oi_status == options.OI_UNAVAILABLE and s.pcr_oi is None


def test_both_sides_missing_is_unavailable_and_votes_nothing():
    s = options.summarise(chain([np.nan] * 3, [np.nan] * 3), 24_000.0)
    assert s.bias == options.BIAS_UNAVAILABLE and s.max_pain is None
    assert s.bias not in ("bullish", "bearish", "neutral")


def test_partial_strikes_use_only_the_paired_ones_and_say_so():
    s = options.summarise(chain([100.0, np.nan, 300.0], [200.0, 999.0, 150.0]),
                          24_000.0)
    assert s.oi_status == options.OI_PARTIAL
    assert (s.oi_paired_strikes, s.oi_total_strikes) == (2, 3)
    # (200 + 150) / (100 + 300); the unpaired 999 is not counted.
    assert s.pcr_oi == pytest.approx(0.875)


def test_a_genuine_zero_is_a_reading_and_differs_from_missing():
    zero_puts = options.summarise(chain([100.0, 200.0, 300.0], [0.0] * 3), 24_000.0)
    assert zero_puts.oi_status == options.OI_AVAILABLE
    assert zero_puts.pcr_oi == 0.0                        # recorded, and real

    zero_calls = options.summarise(chain([0.0] * 3, [100.0, 200.0, 300.0]), 24_000.0)
    assert zero_calls.pcr_oi is None
    assert zero_calls.pcr_status == "zero_call_oi"        # undefined, not missing
    assert zero_calls.oi_status == options.OI_AVAILABLE


def test_the_archive_stores_missing_oi_as_null_and_zero_as_zero(db, monkeypatch):
    payload = nse_payload()
    payload["records"]["data"][0]["CE"].pop("openInterest")
    payload["records"]["data"][0]["PE"]["openInterest"] = 0
    parsed, _ = parse_option_chain(payload)
    import_poll(db, parsed, utc(DAY, 10, 1), monkeypatch)

    rows = {c.option_type: o for o, c in db.execute(
        select(OptionCandle, OptionContract).join(OptionContract)).all()}
    assert rows["CE"].open_interest is None
    assert rows["PE"].open_interest == 0.0


# ===========================================================================
# 10  forward option-data collection
# ===========================================================================

def test_the_collector_keeps_the_touch_depth_and_clocks_the_source_sent(db, monkeypatch):
    parsed, _ = parse_option_chain(nse_payload(bidprice=101.0, askPrice=102.0,
                                               bidQty=750, askQty=1500))
    parsed.attrs["source_time"] = ist(DAY, 10, 0, 58).isoformat()
    import_poll(db, parsed, utc(DAY, 10, 1), monkeypatch)

    ce = db.execute(select(OptionCandle).join(OptionContract).where(
        OptionContract.option_type == "CE")).scalar_one()
    assert (ce.bid, ce.ask, ce.bid_size, ce.ask_size) == (101.0, 102.0, 750.0, 1500.0)
    assert ce.exchange_time is not None and ce.capture_time is not None
    assert ce.first_seen is not None
    # The put side carried no touch: unavailable, not zero.
    pe = db.execute(select(OptionCandle).join(OptionContract).where(
        OptionContract.option_type == "PE")).scalar_one()
    assert (pe.bid, pe.ask, pe.bid_size) == (None, None, None)


def test_a_crossed_book_is_not_stored_as_a_quote(db, monkeypatch):
    parsed, _ = parse_option_chain(nse_payload(bidprice=103.0, askPrice=102.0))
    import_poll(db, parsed, utc(DAY, 10, 1), monkeypatch)
    ce = db.execute(select(OptionCandle).join(OptionContract).where(
        OptionContract.option_type == "CE")).scalar_one()
    assert ce.bid is None and ce.ask is None


def test_a_contract_gets_no_lot_size_the_source_did_not_publish(db, monkeypatch):
    parsed, _ = parse_option_chain(nse_payload())
    import_poll(db, parsed, utc(DAY, 10, 1), monkeypatch)
    assert {c.lot_size for c in db.scalars(select(OptionContract))} == {None}


def test_a_later_poll_without_a_lot_size_does_not_erase_one(db, monkeypatch):
    parsed, _ = parse_option_chain(nse_payload())
    monkeypatch.setattr(importer, "datetime", frozen(ist(DAY, 10, 2)))
    importer.import_option_snapshot(db, parsed, underlying="NIFTY",
                                    expiry="23-Jun-2026", spot=24_010.0,
                                    source="angel",
                                    captured_at=utc(DAY, 10, 1),
                                    lot_size=65)
    import_poll(db, parsed, utc(DAY, 10, 1, 30), monkeypatch)
    assert {c.lot_size for c in db.scalars(select(OptionContract))} == {65}


# ===========================================================================
# 11  the research-readiness report
# ===========================================================================

def test_the_readiness_report_counts_per_session_and_blocks_quoteless_execution(
        db, monkeypatch):
    frame = full_session()
    frame = frame[frame["timestamp"] != ist(DAY, 9, 20).tz_convert("UTC")]
    for r in frame.itertuples():
        db.add(CandleRecord(symbol="NIFTY", timeframe="5m",
                            timestamp=r.timestamp.to_pydatetime(), open=r.open,
                            high=r.high, low=r.low, close=r.close, volume=r.volume,
                            source="test", session_date=DAY))
    db.commit()
    parsed, _ = parse_option_chain(nse_payload())
    import_poll(db, parsed, utc(DAY, 10, 1), monkeypatch)

    report = readiness.report(db)
    session = report["sessions"][0]

    assert (session["expected_bars"], session["received_bars"],
            session["missing_bars"]) == (75, 74, 1)
    assert session["index_status"] == readiness.FAULTY
    assert session["option_bars"] == 2 and session["option_snapshots"] == 1
    assert session["bid_ask_coverage_pct"] == 0.0
    assert session["oi_coverage_pct"] == 100.0
    assert session["capture_clock_coverage_pct"] == 100.0
    assert session["contract_metadata_coverage_pct"] == 0.0
    assert report["verdicts"]["option_execution"] == readiness.BLOCKED
