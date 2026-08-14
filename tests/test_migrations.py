"""Tests for the schema migrations.

`Base.metadata.create_all` silently does nothing to a table that already
exists. That is fine for a fresh test database and actively dangerous for
the live one: the new provenance columns would never appear, the importer
would fail on every write, and the failure would arrive at runtime rather
than at deploy time.

So the migrations are tested the way they will actually be used — against a
database that already holds rows in the old shape.
"""
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

alembic_config = pytest.importorskip(
    "alembic.config", reason="alembic is required to test migrations")
from alembic import command  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1] / "backend"
IST = timezone(timedelta(hours=5, minutes=30))


def make_config(url: str):
    cfg = alembic_config.Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'migrate.db'}"


def test_fresh_database_upgrades_to_head(url):
    """The path a new install takes."""
    command.upgrade(make_config(url), "head")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert {"signals", "trades", "candles", "option_contracts",
            "option_candles", "dataset_versions"} <= tables

    columns = {c["name"] for c in inspect(engine).get_columns("candles")}
    assert {"session_date", "ingested_at", "volume_is_synthetic",
            "revision", "source"} <= columns
    engine.dispose()


def test_head_matches_the_models(url):
    """A migration chain that drifts from models.py is worse than none: the
    app reads one schema and the database holds another, and nothing says
    so until a query fails in production."""
    from app.models import Base

    command.upgrade(make_config(url), "head")
    migrated = create_engine(url)

    modelled = create_engine("sqlite://")
    Base.metadata.create_all(modelled)

    for table in Base.metadata.tables:
        got = {c["name"] for c in inspect(migrated).get_columns(table)}
        want = {c["name"] for c in inspect(modelled).get_columns(table)}
        assert got == want, f"{table}: migration and models disagree"

    migrated.dispose()
    modelled.dispose()


def test_existing_archive_is_migrated_not_discarded(url):
    """The case that matters. A database already holding real candles gets
    stamped at the baseline and upgraded on top — the rows survive, and the
    new columns are derived from what is already there."""
    cfg = make_config(url)
    command.upgrade(cfg, "0001")

    engine = create_engine(url)
    # A session of bars whose volume is a constant placeholder, and one row
    # with no source at all — both shapes that exist in the real archive.
    with engine.begin() as conn:
        for i in range(5):
            moment = datetime(2026, 6, 16, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
            conn.execute(text("""
                INSERT INTO candles
                  (symbol, timeframe, timestamp, open, high, low, close, volume, source)
                VALUES ('NIFTY', '5m', :ts, 24000, 24010, 23990, 24005, 1.0, :src)
            """), {"ts": moment.astimezone(UTC).replace(tzinfo=None),
                   "src": None if i == 0 else "free"})

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT session_date, volume_is_synthetic, source, revision "
            "FROM candles ORDER BY timestamp")).fetchall()

    assert len(rows) == 5, "existing candles must survive the migration"
    # 09:15 IST on the 16th is 03:45 UTC the same day — the session date has
    # to come from the IST clock, or sessions split across two dates.
    assert all(str(r[0]) == "2026-06-16" for r in rows)
    assert all(bool(r[1]) for r in rows), "constant volume must be flagged synthetic"
    assert rows[0][2] == "unknown", "a row with no source is labelled, not assumed good"
    assert all(r[3] == 0 for r in rows)
    engine.dispose()


def test_real_volume_is_not_flagged_as_synthetic(url):
    """The other half of the backfill: genuine volume must survive it
    unlabelled, or every volume-based check gets disabled on good data."""
    cfg = make_config(url)
    command.upgrade(cfg, "0001")

    engine = create_engine(url)
    with engine.begin() as conn:
        for i in range(5):
            moment = datetime(2026, 6, 16, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
            conn.execute(text("""
                INSERT INTO candles
                  (symbol, timeframe, timestamp, open, high, low, close, volume, source)
                VALUES ('NIFTY', '5m', :ts, 24000, 24010, 23990, 24005, :vol, 'kite')
            """), {"ts": moment.astimezone(UTC).replace(tzinfo=None),
                   "vol": 1000.0 + i})

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        flags = conn.execute(text("SELECT volume_is_synthetic FROM candles")).fetchall()
    assert not any(bool(f[0]) for f in flags)
    engine.dispose()


def test_upgrade_survives_a_database_built_by_create_all(url):
    """`init_db()` calls `Base.metadata.create_all`, so a deployment that
    starts the app before running migrations arrives at 0001 with tables
    that already exist in their final shape. Failing there would leave a
    half-migrated schema and no obvious way forward."""
    from app.models import Base

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()

    command.upgrade(make_config(url), "head")

    engine = create_engine(url)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0003"
    engine.dispose()


def test_downgrade_returns_to_the_baseline(url):
    """An unreversible migration is one nobody dares apply."""
    cfg = make_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert "option_candles" not in tables
    columns = {c["name"] for c in inspect(engine).get_columns("candles")}
    assert "session_date" not in columns
    engine.dispose()
