"""Is the database at the schema this code expects? Reads; never migrates.

Starting the desk used to run `alembic upgrade head` on the way up. That is
how migration 0010 reached the live database on 28 Sep: nobody decided to
apply it, a restart did. A schema change is a deployment decision, taken
with the writers paused and a backup in hand (`scripts/migrate.sh`), so
startup now only asks whether the database is where the code expects it
and refuses to start the API when it is not:

    at_head            the one revision this checkout's chain ends at — start
    behind             an older revision of this chain — migrate deliberately
    unversioned        no alembic_version at all — migrate deliberately
    unknown_revision   a revision this checkout does not know (a newer
                       release's, or another project's) — never guess
    unreachable        no answer from the database — nothing is known

The check opens PostgreSQL read-only, so it cannot write even by mistake.

    python -m app.schema_check          exit status is the verdict's code
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, make_url
from sqlalchemy.pool import NullPool

from .redact import describe, error_text

BACKEND = Path(__file__).resolve().parents[1]

AT_HEAD = "at_head"
BEHIND = "behind"
UNVERSIONED = "unversioned"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown_revision"

EXIT_CODES = {AT_HEAD: 0, BEHIND: 1, UNVERSIONED: 2, UNREACHABLE: 3, UNKNOWN: 4}


@dataclass(frozen=True)
class SchemaStatus:
    state: str
    current: str | None
    expected: str
    pending: tuple[str, ...]
    message: str

    @property
    def ok(self) -> bool:
        return self.state == AT_HEAD

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.state]


def _scripts() -> ScriptDirectory:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def expected_head(scripts: ScriptDirectory | None = None) -> str:
    """The single revision this checkout's migration chain ends at."""
    heads = (scripts or _scripts()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"the migration chain has {len(heads)} heads {heads}; "
                           "merge them before deploying")
    return heads[0]


def _current_heads(url: str, connect_timeout: int) -> tuple[str, ...]:
    connect_args = {}
    if make_url(url).get_backend_name() == "postgresql":
        connect_args = {"connect_timeout": connect_timeout,
                        "options": "-c default_transaction_read_only=on"}
    engine = create_engine(url, connect_args=connect_args, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            heads = MigrationContext.configure(conn).get_current_heads()
            conn.rollback()
            return tuple(heads)
    finally:
        engine.dispose()


def check(url: str | None = None, *, connect_timeout: int = 5) -> SchemaStatus:
    """Compare the database's revision with this checkout's head."""
    if url is None:
        from .config import get_settings
        url = get_settings().database_url
    # Never the URL itself: it may carry a password in the authority or the
    # query string. Host, port and database only (Pass 2E-A.1).
    where = describe(url)
    try:
        heads = _current_heads(url, connect_timeout)
    except Exception as exc:  # noqa: BLE001 — any failure to read is "unknown"
        return SchemaStatus(UNREACHABLE, None, expected_head(), (),
                            f"cannot read the schema revision of {where}: "
                            f"{error_text(exc, url)}")
    return _verdict(heads, where)


def check_connection(conn, url) -> SchemaStatus:
    """The same verdict, read on a connection the caller already holds — the
    migration reads it on the very connection that owns the exclusive lock
    and runs Alembic (Pass 2E-A.2). The read transaction is ended before
    returning, so the connection is idle for whatever runs next."""
    heads = tuple(MigrationContext.configure(conn).get_current_heads())
    conn.rollback()
    return _verdict(heads, describe(url))


def _verdict(heads: tuple[str, ...], where: str) -> SchemaStatus:
    scripts = _scripts()
    head = expected_head(scripts)
    migrate = "stop the API, back up the database, then run ./scripts/migrate.sh"

    if not heads:
        return SchemaStatus(UNVERSIONED, None, head, (),
                            f"{where} has no alembic_version; this code expects "
                            f"revision {head}. To build it: {migrate}")
    if len(heads) > 1:
        return SchemaStatus(UNKNOWN, ",".join(heads), head, (),
                            f"{where} records several revisions {heads}; this code "
                            f"expects {head} alone. Investigate before starting")
    current = heads[0]
    if current == head:
        return SchemaStatus(AT_HEAD, current, head, (),
                            f"{where} is at revision {current}, as this code expects")

    try:
        known = scripts.get_revision(current) is not None
        pending = tuple(reversed([r.revision for r in scripts.iterate_revisions(head, current)
                                  if r.revision != current])) if known else ()
    except CommandError:
        known, pending = False, ()
    if not known or not pending:
        return SchemaStatus(UNKNOWN, current, head, (),
                            f"{where} is at revision {current}, which this checkout "
                            f"(head {head}) does not know — a newer release or another "
                            "project migrated it. Do not start this code against it")
    named = "; ".join(f"{r} {scripts.get_revision(r).doc}" for r in pending)
    return SchemaStatus(BEHIND, current, head, pending,
                        f"{where} is at revision {current}; this code expects {head} "
                        f"(pending: {named}). {migrate}")


def main() -> int:
    status = check()
    print(status.message, file=sys.stdout if status.ok else sys.stderr)
    return status.exit_code


if __name__ == "__main__":
    from .redact import run_cli
    sys.exit(run_cli(main))
