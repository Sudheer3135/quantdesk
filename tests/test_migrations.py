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


def head_revision() -> str:
    """The newest revision in the chain, read from the scripts themselves."""
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(make_config("sqlite://")).get_current_head()


def test_the_head_lookup_finds_a_real_revision():
    """Guard against the guard: a helper that returned None would make the
    assertion below pass against a database that migrated nowhere."""
    head = head_revision()
    assert head and head.isdigit(), head


def test_trade_exit_migration_preserves_unknown_close_times(url):
    cfg = make_config(url)
    command.upgrade(cfg, "0007")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO trades "
                          "(symbol, side, quantity, entry, stop_loss, status, pnl) "
                          "VALUES ('NIFTY', 'BUY', 65, 200, 150, 'closed', -6500)"))
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT pnl, closed_at FROM trades")).one() == (-6500, None)
    assert "ix_trades_closed_at" in {
        index["name"] for index in inspect(engine).get_indexes("trades")}
    command.downgrade(cfg, "0007")
    assert "closed_at" not in {c["name"] for c in inspect(engine).get_columns("trades")}
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
    # Derived, not hardcoded. A pinned number turns "we added a migration"
    # into a test failure that says nothing about migrations working.
    assert version == head_revision()
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


# ---- 0005: market regimes ----------------------------------------------

def test_the_regime_table_is_created_with_its_indexes(url):
    command.upgrade(make_config(url), "head")

    engine = create_engine(url)
    inspector = inspect(engine)
    assert "market_regimes" in set(inspector.get_table_names())

    columns = {c["name"] for c in inspector.get_columns("market_regimes")}
    assert {"symbol", "timeframe", "timestamp", "session_date",
            "day_regime", "day_confidence", "day_reasons",
            "hour_regime", "hour_confidence", "hour_reasons",
            "features", "engine_version", "computed_at"} <= columns

    indexes = {ix["name"] for ix in inspector.get_indexes("market_regimes")}
    assert "ix_regime_session" in indexes
    engine.dispose()


def test_the_regime_table_survives_a_database_that_already_had_it(url):
    """The awkward upgrade state: the app booted, `create_all` built the
    table, and only then did anyone run the migration. A create that assumed
    it was starting from nothing would fail here with "table already
    exists" and leave the schema half-applied."""
    from app.models import Base

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()

    command.upgrade(make_config(url), "head")       # must not raise

    engine = create_engine(url)
    assert "market_regimes" in set(inspect(engine).get_table_names())
    engine.dispose()


def test_the_regime_migration_is_reversible(url):
    cfg = make_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert "market_regimes" not in tables
    # The migration before it is untouched — a downgrade must not take
    # neighbouring work with it.
    assert "risk" in {c["name"] for c in inspect(engine).get_columns("signals")}
    engine.dispose()


def test_a_regime_row_round_trips_through_the_migrated_schema(url):
    """The columns exist is not the same claim as the columns work. JSON on
    SQLite in particular is a text column with a converter, and a mismatch
    between the migration's type and the model's shows up only on a write."""
    import json

    command.upgrade(make_config(url), "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO market_regimes (symbol, timeframe, timestamp, "
            "session_date, day_regime, day_confidence, day_reasons, "
            "hour_regime, hour_confidence, hour_reasons, features, "
            "engine_version, computed_at) VALUES "
            "('NIFTY', '5m', :ts, :day, 'TREND_UP', 0.72, :reasons, "
            "'RANGE', 0.4, :reasons, :features, '1.0', :ts)"),
            {"ts": datetime(2026, 6, 17, 4, 30, tzinfo=UTC).isoformat(),
             "day": "2026-06-17",
             "reasons": json.dumps(["ATR is 1.3x its own average."]),
             "features": json.dumps({"day": {"efficiency": 0.7}})})

        row = conn.execute(text(
            "SELECT day_regime, day_confidence FROM market_regimes")).one()
    assert row == ("TREND_UP", 0.72)
    engine.dispose()


def test_running_a_migration_does_not_silence_the_application(url):
    """Alembic must not mute the desk on its way past.

    `alembic/env.py` calls `logging.config.fileConfig`, whose default is
    `disable_existing_loggers=True`. That sets `.disabled = True` on every
    logger not named in alembic.ini — which is every `app.*` logger this
    project has. A process that migrated in-process then went permanently
    silent: no agent tick, no collector failure, and no scheduler-starvation
    alarm, all while the desk carried on running and looking healthy.

    Today's compose command runs the migration as a separate process from
    uvicorn, so this was not costing production anything. It was costing the
    suite: the tests proving the starvation alarm actually reaches a log were
    failing because alembic had muted the logger several files earlier, which
    is exactly how a real regression would present.
    """
    import logging

    from app.workers import watchdog

    command.upgrade(make_config(url), "head")

    assert not watchdog.log.disabled, "alembic disabled the application's loggers"

    # A handler on the logger itself rather than `caplog`. `fileConfig` also
    # rebuilds the *root* logger's handlers, which is where caplog attaches,
    # and that part is legitimate — alembic is a CLI and configuring root is
    # its job. What must survive is the app logger's ability to emit at all.
    captured: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = Collect()
    watchdog.log.addHandler(handler)
    try:
        watchdog.log.warning("the desk can still speak")
    finally:
        watchdog.log.removeHandler(handler)

    assert captured == ["the desk can still speak"]
