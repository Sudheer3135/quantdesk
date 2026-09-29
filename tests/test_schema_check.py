"""Startup checks the schema revision; it never migrates (Pass 2E-A).

`scripts/start.sh` used to run `alembic upgrade head`, which is how
migration 0010 reached the live database without a deployment decision.
Startup now runs `app.schema_check` and refuses to start the API unless
the database is at exactly this checkout's head — behind, unversioned,
unknown and unreachable all fail closed.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

alembic_config = pytest.importorskip("alembic.config")
from alembic import command  # noqa: E402
from app import schema_check  # noqa: E402

HEAD = schema_check.expected_head()
BACKENDS = ["sqlite"] + (["postgresql"] if os.getenv("TEST_MIGRATION_DATABASE_URL") else [])


def config(url: str):
    cfg = alembic_config.Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(params=BACKENDS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'schema.db'}"
        return
    pg = os.environ["TEST_MIGRATION_DATABASE_URL"]

    def wipe():
        engine = create_engine(pg)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()

    wipe()
    yield pg
    wipe()


def stamp(url: str, revision: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": revision})
    engine.dispose()


def test_the_expected_head_is_the_newest_migration():
    newest = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("0*.py"))[-1]
    assert newest.startswith(HEAD + "_"), (newest, HEAD)


def test_a_database_at_head_is_allowed(db_url):
    command.upgrade(config(db_url), "head")
    status = schema_check.check(db_url)
    assert status.ok and status.state == schema_check.AT_HEAD
    assert status.current == HEAD and status.exit_code == 0


def test_a_database_behind_head_is_refused_and_names_what_is_pending(db_url):
    command.upgrade(config(db_url), "0009")
    status = schema_check.check(db_url)
    assert not status.ok and status.state == schema_check.BEHIND
    assert status.current == "0009" and status.pending[-1] == HEAD
    assert status.exit_code == 1 and "migrate.sh" in status.message
    # The check read the revision and nothing else: still at 0009.
    assert schema_check.check(db_url).current == "0009"
    assert "research_events" not in inspect(create_engine(db_url)).get_table_names()


def test_a_database_ahead_of_or_foreign_to_this_checkout_is_refused(db_url):
    command.upgrade(config(db_url), "head")
    stamp(db_url, "9999")                       # a revision this checkout never wrote
    status = schema_check.check(db_url)
    assert not status.ok and status.state == schema_check.UNKNOWN
    assert status.current == "9999" and status.exit_code == 4


def test_a_database_with_no_version_table_is_refused_and_not_created(db_url):
    status = schema_check.check(db_url)
    assert not status.ok and status.state == schema_check.UNVERSIONED
    assert status.exit_code == 2
    assert "alembic_version" not in inspect(create_engine(db_url)).get_table_names()


def test_several_recorded_revisions_are_refused(db_url):
    command.upgrade(config(db_url), "head")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0009')"))
    engine.dispose()
    status = schema_check.check(db_url)
    assert not status.ok and status.state == schema_check.UNKNOWN


def test_an_unreachable_database_is_refused_clearly():
    # Port 1 on the loopback: nothing listens, so the refusal is immediate.
    url = "postgresql+psycopg://quant:secret-pw@127.0.0.1:1/quantdesk"
    status = schema_check.check(url, connect_timeout=2)
    assert not status.ok and status.state == schema_check.UNREACHABLE
    assert status.exit_code == 3 and "127.0.0.1:1" in status.message
    assert "secret-pw" not in status.message


def run_cli(url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "app.schema_check"], cwd=BACKEND,
                          env=env, capture_output=True, text=True, timeout=60)


def test_the_command_start_sh_runs_exits_by_verdict(tmp_path):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    assert run_cli(url).returncode == 2                      # unversioned
    command.upgrade(config(url), "0009")
    behind = run_cli(url)
    assert behind.returncode == 1 and "0009" in behind.stderr
    command.upgrade(config(url), "head")
    assert run_cli(url).returncode == 0
    assert run_cli("postgresql+psycopg://q:q@127.0.0.1:1/x").returncode == 3


def test_starting_the_desk_checks_the_schema_and_never_migrates():
    code = [line for line in (ROOT / "scripts" / "start.sh").read_text().splitlines()
            if not line.lstrip().startswith("#")]
    assert not any("alembic" in line for line in code), "start.sh must not run alembic"
    assert any("app.schema_check" in line for line in code)
    # Writer safety is the database's schema lock (app.migrate), not a port.
    migrate = (ROOT / "scripts" / "migrate.sh").read_text()
    assert "app.migrate" in migrate and "our_listener" not in migrate
