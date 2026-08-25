"""Storing the regime history and reading it back.

The interesting question here is not "does the insert work". It is whether
the incremental pass the agent runs every five minutes agrees with the full
backfill. Classification is causal but not memoryless — ATR is a Wilder
average, VWAP is anchored to the session open, the ATR baseline looks back a
hundred bars — so classifying the newest bar in isolation would produce a
different answer from classifying it inside the whole archive, and the table
would quietly end up holding two definitions of the same label.

`test_an_incremental_pass_agrees_with_a_full_backfill` is what makes the
lookback constant a claim rather than a hope.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import regime
from app.data import regime_store
from app.data.importer import import_index_candles
from app.market_hours import IST
from app.models import MarketRegime

BARS_PER_SESSION = 75


def candle_frame(n, seed=1, drift=0.0, scale=6.0):
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, scale, n)
    close = 24_000 + np.cumsum(steps)
    day = datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    stamps = []
    for i in range(n):
        stamps.append((day + timedelta(minutes=5 * (i % BARS_PER_SESSION)))
                      .astimezone(UTC))
        if (i + 1) % BARS_PER_SESSION == 0:
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
    return pd.DataFrame({
        "timestamp": stamps, "open": close - steps,
        "high": np.maximum(close, close - steps) + 2,
        "low": np.minimum(close, close - steps) - 2,
        "close": close, "volume": [1000.0] * n,
    })


@pytest.fixture
def archive(db):
    """A stored archive of five-minute candles, as the agent would build it."""
    frame = candle_frame(400)
    import_index_candles(db, frame, "NIFTY", "5m", source="test")
    return frame


# ---- the backfill ------------------------------------------------------

def test_backfill_classifies_every_stored_candle(db, archive):
    report = regime_store.backfill(db, "NIFTY", "5m")

    assert report.candles == len(archive)
    assert report.classified == len(archive)
    assert report.inserted == len(archive)

    # Compared through `load`, which normalises the timestamp: reading the
    # model attribute directly would compare naive values on SQLite against
    # aware ones on Postgres and pass on neither for the same reason.
    stored = regime_store.load(db, "NIFTY", "5m")
    assert len(stored) == len(archive)
    assert list(stored["timestamp"]) == list(
        pd.to_datetime(archive["timestamp"], utc=True))


def test_backfill_stores_both_levels_with_their_reasons(db, archive):
    regime_store.backfill(db, "NIFTY", "5m")
    row = db.scalars(
        select(MarketRegime).order_by(MarketRegime.timestamp.desc())).first()

    assert row.day_regime in regime.LABELS
    assert row.hour_regime in regime.LABELS
    assert row.day_reasons and row.hour_reasons
    assert row.features["day"] and row.features["hour"]
    assert row.features["day_scores"]
    assert row.engine_version == regime.ENGINE_VERSION


def test_backfill_is_idempotent(db, archive):
    first = regime_store.backfill(db, "NIFTY", "5m")
    second = regime_store.backfill(db, "NIFTY", "5m")

    assert first.inserted == len(archive)
    assert second.inserted == 0
    assert second.updated == len(archive)
    assert len(db.scalars(select(MarketRegime)).all()) == len(archive)


def test_re_running_the_backfill_does_not_change_any_verdict(db, archive):
    """Idempotent in content, not merely in row count."""
    regime_store.backfill(db, "NIFTY", "5m")
    before = {r.timestamp: (r.day_regime, r.day_confidence, r.hour_regime)
              for r in db.scalars(select(MarketRegime)).all()}

    regime_store.backfill(db, "NIFTY", "5m")
    after = {r.timestamp: (r.day_regime, r.day_confidence, r.hour_regime)
             for r in db.scalars(select(MarketRegime)).all()}

    assert before == after


def test_backfill_on_an_empty_archive_says_so_rather_than_failing(db):
    report = regime_store.backfill(db, "NIFTY", "5m")

    assert report.candles == 0
    assert report.classified == 0
    assert "nothing to classify" in (report.note or "")


def test_backfill_reports_the_label_mix(db, archive):
    report = regime_store.backfill(db, "NIFTY", "5m")

    assert sum(report.day_labels.values()) == len(archive)
    assert set(report.day_labels) <= set(regime.LABELS)


# ---- the incremental pass ----------------------------------------------

def test_an_incremental_pass_agrees_with_a_full_backfill(db, archive):
    """The claim `INCREMENTAL_LOOKBACK_BARS` is chosen to make true.

    ATR(14) is an EWM with alpha 1/14, so after 750 bars the starting value's
    influence is (13/14)^750 — far below floating-point resolution — and ten
    sessions comfortably contains the current session's VWAP anchor and the
    previous session's close. Within that window the two paths must produce
    the same verdicts, not merely similar ones.
    """
    regime_store.backfill(db, "NIFTY", "5m")
    full = {r.timestamp: (r.day_regime, r.day_confidence,
                          r.hour_regime, r.hour_confidence)
            for r in db.scalars(select(MarketRegime)).all()}

    regime_store.clear(db, "NIFTY", "5m")
    regime_store.refresh_recent(db, "NIFTY", "5m", keep_last=40)

    incremental = db.scalars(select(MarketRegime)).all()
    assert len(incremental) == 40
    for row in incremental:
        assert (row.day_regime, row.day_confidence,
                row.hour_regime, row.hour_confidence) == full[row.timestamp]


def test_refresh_only_writes_the_tail(db, archive):
    report = regime_store.refresh_recent(db, "NIFTY", "5m", keep_last=12)

    assert report.classified == 12
    assert len(db.scalars(select(MarketRegime)).all()) == 12


def test_refresh_on_an_empty_archive_is_a_no_op(db):
    report = regime_store.refresh_recent(db, "NIFTY", "5m")

    assert report.classified == 0
    assert db.scalars(select(MarketRegime)).all() == []


def test_refresh_updates_a_bar_it_has_already_seen(db, archive):
    regime_store.refresh_recent(db, "NIFTY", "5m", keep_last=20)
    regime_store.refresh_recent(db, "NIFTY", "5m", keep_last=20)

    assert len(db.scalars(select(MarketRegime)).all()) == 20


# ---- reading it back ---------------------------------------------------

def test_load_returns_utc_aware_timestamps_on_both_backends(db, archive):
    """SQLite hands these back naive and Postgres aware. A join against
    signal timestamps would then match on one backend and raise on the
    other — the same divergence that once silently emptied the outcome
    study on SQLite alone."""
    regime_store.backfill(db, "NIFTY", "5m")
    frame = regime_store.load(db, "NIFTY", "5m")

    assert len(frame) == len(archive)
    assert str(frame["timestamp"].dt.tz) == "UTC"
    assert frame["timestamp"].is_monotonic_increasing


def test_load_on_an_empty_table_returns_an_empty_frame_with_columns(db):
    frame = regime_store.load(db, "NIFTY", "5m")

    assert frame.empty
    assert "day_regime" in frame.columns


def test_latest_is_the_most_recent_bar(db, archive):
    regime_store.backfill(db, "NIFTY", "5m")
    found = regime_store.latest(db, "NIFTY", "5m")
    computed = regime.classify_latest(archive)

    assert found["day"]["label"] == computed["day"]["label"]
    assert found["hour"]["label"] == computed["hour"]["label"]
    assert found["day"]["reasons"]
    assert found["engine_version"] == regime.ENGINE_VERSION


def test_latest_is_none_when_nothing_is_classified(db):
    assert regime_store.latest(db, "NIFTY", "5m") is None


def test_regimes_are_kept_apart_by_symbol(db, archive):
    regime_store.backfill(db, "NIFTY", "5m")

    assert regime_store.latest(db, "BANKNIFTY", "5m") is None
    assert regime_store.coverage(db, "BANKNIFTY", "5m")["rows"] == 0


# ---- version hygiene ---------------------------------------------------

def test_coverage_reports_which_engine_versions_are_present(db, archive):
    regime_store.backfill(db, "NIFTY", "5m")
    found = regime_store.coverage(db, "NIFTY", "5m")

    assert found["rows"] == len(archive)
    assert found["engine_versions"] == {regime.ENGINE_VERSION: len(archive)}
    assert found["mixed_versions"] is False


def test_a_table_holding_two_engine_versions_is_flagged(db, archive):
    """The failure `engine_version` exists to catch. A regime split computed
    across two definitions of TREND_UP would look like a finding."""
    regime_store.backfill(db, "NIFTY", "5m")
    row = db.scalars(select(MarketRegime).limit(1)).first()
    row.engine_version = "0.9"
    db.commit()

    found = regime_store.coverage(db, "NIFTY", "5m")
    assert found["mixed_versions"] is True
    assert set(found["engine_versions"]) == {regime.ENGINE_VERSION, "0.9"}


def test_clear_removes_only_the_named_series(db, archive):
    regime_store.backfill(db, "NIFTY", "5m")
    removed = regime_store.clear(db, "NIFTY", "5m")

    assert removed == len(archive)
    assert regime_store.coverage(db, "NIFTY", "5m")["rows"] == 0


def test_coverage_on_an_empty_table_points_at_the_backfill(db):
    found = regime_store.coverage(db, "NIFTY", "5m")

    assert found["rows"] == 0
    assert "backfill" in (found["note"] or "")
