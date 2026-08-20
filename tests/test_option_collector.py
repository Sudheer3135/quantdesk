"""Tests for the option-chain collector.

The property that matters: polling faster than the bar width must produce
bars that *aggregate*, not bars that multiply. Five polls inside one
five-minute bucket is one bar with five samples, an open from the first
poll, extremes across all of them, and a close from the last.

Before this, the collector ran at the same 5-minute cadence as the bar, so
every bar held exactly one observation and its high and low equalled its
close. The range was fiction. These tests pin down that it no longer is.
"""
import sys
from datetime import UTC, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings
from app.data.importer import import_option_snapshot
from app.models import OptionCandle, OptionContract
from test_option_data import EXPIRY, MOMENT, chain


def poll(db, at, ltp):
    """One collector poll landing at `at` with a given call premium."""
    return import_option_snapshot(
        db, chain(call_ltp=ltp), underlying="NIFTY", expiry=EXPIRY,
        spot=24_450.0, source="free", captured_at=at, timeframe="5m")


def ce_bar(db, strike=24_400):
    return db.scalars(
        select(OptionCandle).join(OptionContract)
        .where(OptionContract.option_type == "CE",
               OptionContract.strike == strike)).first()


# ---- aggregation ------------------------------------------------------

def test_one_minute_polling_builds_one_bar_with_five_samples(db):
    """The whole point of F2. Five polls, one bar."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)   # 05:20 bucket start
    for i in range(5):
        poll(db, base + timedelta(minutes=i), ltp=100.0 + i)

    bars = db.scalars(select(OptionCandle)).all()
    timestamps = {b.timestamp for b in bars}
    assert len(timestamps) == 1, "five polls must fold into one bucket"
    assert all(b.samples == 5 for b in bars)


def test_the_aggregated_bar_has_a_real_range(db):
    """open = first, high = max, low = min, close = latest. Without this the
    high and low equal the close and the bar describes nothing."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)
    for offset, ltp in enumerate([100.0, 130.0, 85.0, 120.0, 110.0]):
        poll(db, base + timedelta(minutes=offset), ltp=ltp)

    bar = ce_bar(db)
    assert bar.open == 100.0, "the first poll opened the bar and must not be rewritten"
    assert bar.high == 130.0
    assert bar.low == 85.0
    assert bar.close == 110.0, "the last poll sets the close"
    assert bar.samples == 5
    assert bar.high > bar.low, "an aggregated bar must have a range"


def test_polls_crossing_a_bucket_boundary_start_a_new_bar(db):
    """Minute 4 and minute 5 belong to different five-minute bars."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)
    poll(db, base + timedelta(minutes=4), ltp=100.0)
    poll(db, base + timedelta(minutes=5), ltp=200.0)

    bars = sorted(db.scalars(
        select(OptionCandle).join(OptionContract)
        .where(OptionContract.option_type == "CE",
               OptionContract.strike == 24_400)).all(),
        key=lambda b: b.timestamp)

    assert len(bars) == 2
    assert bars[0].timestamp != bars[1].timestamp
    assert bars[0].close == 100.0
    assert bars[1].open == 200.0
    assert all(b.samples == 1 for b in bars)


def test_bars_stay_labelled_as_snapshots_however_many_samples(db):
    """Five sampled last-traded prices make the range less fictional, not
    real. Nothing downstream may mistake these for a traded tape."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)
    for i in range(5):
        poll(db, base + timedelta(minutes=i), ltp=100.0 + i)

    assert all(b.bar_kind == "snapshot" for b in db.scalars(select(OptionCandle)))


def test_source_and_timestamp_integrity_survive_aggregation(db):
    """F2's explicit requirement. Merging must not relabel provenance or
    drift the bar's timestamp off its bucket boundary."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)
    for i in range(3):
        poll(db, base + timedelta(minutes=i), ltp=100.0 + i)

    bar = ce_bar(db)
    assert bar.source == "free"
    assert bar.timestamp.replace(tzinfo=UTC) == base
    assert bar.timestamp.minute % 5 == 0, "bar must sit on a bucket boundary"
    assert bar.session_date == pd.Timestamp(base).tz_convert("Asia/Kolkata").date()


def test_contracts_are_not_duplicated_by_frequent_polling(db):
    """Polling five times as often must not create five times the
    contracts."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)
    for i in range(5):
        poll(db, base + timedelta(minutes=i), ltp=100.0)

    assert len(db.scalars(select(OptionContract)).all()) == 6   # 3 strikes × CE/PE


def test_revision_counts_the_merges(db):
    """A bar rewritten many times should be able to say so."""
    base = MOMENT.replace(minute=20, second=0, microsecond=0)
    for i in range(4):
        poll(db, base + timedelta(minutes=i), ltp=100.0 + i)

    assert ce_bar(db).revision == 3      # first insert, then three merges


# ---- the worker -------------------------------------------------------

def test_the_collector_does_not_poll_a_closed_market(monkeypatch):
    """Polling a shut market wastes requests on data that cannot change and
    risks being throttled for nothing."""
    from app.workers import option_collector

    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(option_collector, "get_broker",
                        lambda: called.append(1) or object())

    assert option_collector.capture() is None
    assert not called, "the broker must not be touched outside market hours"


def test_the_collector_can_be_forced_for_diagnostics(monkeypatch):
    """`force` exists so a snapshot can be taken manually outside market
    hours. It must still store the result rather than silently discard it —
    the importer flags out-of-hours bars, which is the honest handling."""
    from app.workers import option_collector

    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: False)

    class Broker:
        def chain_with_spot(self, symbol):
            frame = chain()
            frame.attrs["expiry"] = EXPIRY
            return frame, 24_450.0

    stored = {}

    class Report:
        def to_dict(self):
            return {"stored": True}

    def fake_import(db, frame, **kwargs):
        stored["expiry"] = kwargs["expiry"]
        stored["source"] = kwargs["source"]
        return Report()

    monkeypatch.setattr(option_collector, "get_broker", lambda: Broker())
    monkeypatch.setattr(option_collector, "SessionLocal", lambda: _FakeSession({}))
    monkeypatch.setattr(option_collector.importer, "import_option_snapshot", fake_import)

    report = option_collector.capture(force=True)
    assert report == {"stored": True}
    assert stored["expiry"] == EXPIRY, "the labelled expiry must be passed through"
    # Compared against the configured broker rather than a literal: the
    # rule is that provenance is whatever is actually serving the data,
    # never a value invented at the storage layer.
    assert stored["source"] == get_settings().broker


def test_a_chain_without_an_expiry_label_is_refused(monkeypatch):
    """Filing under a guessed expiry silently merges two contracts into one
    premium series, and the result looks entirely plausible."""
    from app.workers import option_collector

    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: True)

    class Broker:
        def chain_with_spot(self, symbol):
            frame = chain()
            frame.attrs.pop("expiry", None)
            return frame, 24_450.0

    monkeypatch.setattr(option_collector, "get_broker", lambda: Broker())
    assert option_collector.capture() is None


def test_a_failed_poll_does_not_kill_the_schedule(monkeypatch):
    """One failed poll costs one observation. An exception escaping `tick`
    would let APScheduler retire the job and cost every observation after
    it — and option history cannot be recovered later."""
    from app.workers import option_collector

    def boom():
        raise RuntimeError("NSE said no")

    monkeypatch.setattr(option_collector, "capture", boom)
    option_collector.tick()          # must not raise


def test_the_poll_interval_is_shorter_than_the_bar_width():
    """The setting that makes aggregation possible at all. If these ever
    match again, every bar silently goes back to a single sample."""
    from app.analytics.indicators import TIMEFRAME_MINUTES
    from app.config import Settings

    settings = Settings()
    bar_seconds = TIMEFRAME_MINUTES[settings.watch_timeframe] * 60
    assert settings.option_snapshot_interval_seconds < bar_seconds, (
        "the collector must poll faster than the bar width, or every bar "
        "holds one observation and its high and low equal its close")


class _FakeSession:
    """Minimal stand-in so `capture` can be exercised without a database.

    The worker tests above check control flow — does it poll, does it
    refuse, does it survive a failure. Storage is covered by the
    aggregation tests at the top of this file, against a real database.
    """

    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        self.sink["called"] = True
        return self

    def __exit__(self, *exc):
        return False
