"""Tests for the read path and dataset fingerprinting.

The fingerprint is the mechanism that turns a backtest result from an
anecdote into a measurement, so the properties it must have are worth
stating as tests rather than assuming: identical data collides, different
data does not, and row order is not data.
"""
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import dataset, repository
from app.data.importer import import_index_candles
from app.models import DatasetVersion
from test_importer import session_bars

IST = timezone(timedelta(hours=5, minutes=30))


def seed(db, days=(16, 17, 18), count=12, **kwargs):
    for d in days:
        import_index_candles(db, session_bars(date(2026, 6, d), count=count, **kwargs),
                             "NIFTY", "5m", "test")


# ---- reading back -----------------------------------------------------

def test_round_trip_preserves_the_candle_shape(db):
    """Whatever else changes, downstream code sees the six columns it has
    always seen — anything else breaks every indicator at once."""
    seed(db, days=(16,))
    df = repository.load_index_candles(db, "NIFTY", "5m")

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 12
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df["timestamp"].is_monotonic_increasing


def test_provenance_travels_with_the_frame_but_not_as_a_column(db):
    """Provenance has to reach the result block without becoming something
    an indexing mistake could treat as a price."""
    seed(db, days=(16,), real_volume=False)
    df = repository.load_index_candles(db, "NIFTY", "5m")

    prov = repository.provenance_of(df)
    assert prov["sources"] == {"test": 12}
    assert prov["volume_is_synthetic"] is True
    assert "source" not in df.columns


def test_date_range_filters_are_inclusive_of_whole_days(db):
    """Asking for a date means the whole session, not up to midnight."""
    seed(db, days=(16, 17, 18))
    df = repository.load_index_candles(
        db, "NIFTY", "5m", start=date(2026, 6, 17), end=date(2026, 6, 17))
    assert len(df) == 12
    assert set(df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.date) == {date(2026, 6, 17)}


def test_empty_result_is_still_the_right_shape(db):
    df = repository.load_index_candles(db, "NIFTY", "5m")
    assert df.empty
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


# ---- coverage ---------------------------------------------------------

def test_coverage_counts_sessions_not_just_rows(db):
    """Rows flatter you; sessions are what a statistic actually rests on."""
    seed(db, days=(16, 17, 18))
    have = repository.coverage(db, "NIFTY", "5m")
    assert have.rows == 36
    assert have.sessions == 3
    assert have.sources == {"test": 36}


def test_coverage_gap_is_reported_rather_than_quietly_served(db):
    """The failure this phase exists to remove: asking for two years,
    silently getting six weeks, and reading the statistics as if they
    answered the question you asked."""
    seed(db, days=(16, 17, 18, 19, 22, 23))

    gap = repository.check_coverage(
        db, "NIFTY", "5m",
        start=datetime(2020, 1, 1, tzinfo=UTC),
        end=datetime(2026, 6, 23, tzinfo=UTC))
    assert gap is not None
    assert "history begins at" in gap.reason
    assert "import" in gap.to_dict()["fix"]


def test_a_window_that_is_held_in_full_passes(db):
    seed(db, days=(16, 17, 18, 19, 22, 23))
    gap = repository.check_coverage(
        db, "NIFTY", "5m",
        start=datetime(2026, 6, 17, tzinfo=UTC),
        end=datetime(2026, 6, 22, tzinfo=UTC))
    assert gap is None


def test_too_few_sessions_is_a_gap_even_when_the_range_fits(db):
    """Four sessions inside the requested window is technically coverage and
    is not enough to mean anything."""
    seed(db, days=(16, 17))
    gap = repository.check_coverage(db, "NIFTY", "5m")
    assert gap is not None
    assert "session" in gap.reason


def test_empty_database_is_a_gap_not_an_empty_backtest(db):
    gap = repository.check_coverage(db, "NIFTY", "5m")
    assert gap is not None
    assert "no candles stored" in gap.reason


# ---- fingerprinting ---------------------------------------------------

def test_the_same_data_hashes_the_same_twice(db):
    """Reproducibility. If this fails, no backtest can be compared to any
    other backtest."""
    seed(db, days=(16, 17))
    a = dataset.fingerprint(repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")
    b = dataset.fingerprint(repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")
    assert a.hash == b.hash


def test_one_changed_tick_changes_the_hash(db):
    """The other half: a hash that does not move when the data moves is
    worse than no hash, because it certifies something false."""
    seed(db, days=(16,))
    before = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")

    restated = session_bars(date(2026, 6, 16), count=12)
    restated.loc[3, ["high", "close"]] = [24_070.0, 24_065.0]
    import_index_candles(db, restated, "NIFTY", "5m", "test")

    after = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")
    assert before.hash != after.hash


def test_extending_the_archive_changes_the_hash(db):
    seed(db, days=(16,))
    before = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")
    seed(db, days=(17,))
    after = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")

    assert before.hash != after.hash
    assert after.row_count == 24


def test_row_order_is_not_data(db):
    """Order is an artefact of the query. Two frames holding the same bars
    are the same dataset."""
    seed(db, days=(16,))
    df = repository.load_index_candles(db, "NIFTY", "5m")
    shuffled = df.sample(frac=1.0, random_state=7)

    assert (dataset.fingerprint(df, "NIFTY", "5m").hash
            == dataset.fingerprint(shuffled, "NIFTY", "5m").hash)


def test_symbol_and_timeframe_are_part_of_the_identity(db):
    """Identical prices on a different instrument are not the same dataset."""
    seed(db, days=(16,))
    df = repository.load_index_candles(db, "NIFTY", "5m")
    assert (dataset.fingerprint(df, "NIFTY", "5m").hash
            != dataset.fingerprint(df, "BANKNIFTY", "5m").hash)
    assert (dataset.fingerprint(df, "NIFTY", "5m").hash
            != dataset.fingerprint(df, "NIFTY", "15m").hash)


def test_float_noise_does_not_change_the_hash(db):
    """A value that round-trips as 24000.000000000004 in one pandas version
    and 24000.0 in another must not invalidate every stored comparison."""
    seed(db, days=(16,))
    df = repository.load_index_candles(db, "NIFTY", "5m")
    noisy = df.copy()
    noisy["close"] = noisy["close"] + 1e-9

    assert (dataset.fingerprint(df, "NIFTY", "5m").hash
            == dataset.fingerprint(noisy, "NIFTY", "5m").hash)


def test_synthetic_volume_and_mock_data_are_called_out(db):
    """A caveat nobody reads is still better than a number nobody
    questions."""
    seed(db, days=(16,), real_volume=False)
    import_index_candles(db, session_bars(date(2026, 6, 17)), "NIFTY", "5m", "mock")

    print_ = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")
    joined = " ".join(print_.caveats)
    assert "placeholder" in joined
    assert "random walk" in joined
    assert "sessions" in joined       # small sample warning


def test_registering_the_same_dataset_twice_stores_one_row(db):
    seed(db, days=(16, 17))
    print_ = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")

    dataset.register(db, print_)
    dataset.register(db, print_)

    rows = db.scalars(select(DatasetVersion)).all()
    assert len(rows) == 1
    assert rows[0].row_count == 24
    assert rows[0].session_count == 2


def test_empty_dataset_hashes_without_crashing(db):
    print_ = dataset.fingerprint(
        repository.load_index_candles(db, "NIFTY", "5m"), "NIFTY", "5m")
    assert print_.row_count == 0
    assert print_.hash
    assert any("empty" in c for c in print_.caveats)


def test_a_frame_from_outside_the_repository_still_fingerprints():
    """Backtests may be handed a broker frame in the explicit escape-hatch
    mode. It has no provenance attached, and that must degrade rather than
    raise."""
    df = session_bars(date(2026, 6, 16))
    print_ = dataset.fingerprint(df, "NIFTY", "5m")
    assert print_.row_count == 12
    assert print_.sources == {}
