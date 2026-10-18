"""Apply pending migrations — the one deliberate way the schema moves.

    python -m app.migrate            dry run: schema state, pending revisions,
                                     writers holding the schema lock
    python -m app.migrate --apply    migrate under the exclusive schema lock

`--apply` opens ONE connection, takes the exclusive schema lock on it
(`migration_guard.MigrationLock`), reads the revision on it, runs
`alembic upgrade head` on it (handed to env.py as
`config.attributes["connection"]`), and reads the revision on it again. The
connection that owns the lock is the connection that migrates: if it dies,
the lock and the migration end together. While it is held, every write
transaction of every supported writer is refused before it writes; while
any write transaction or writer process holds the shared key, `--apply` is
refused (Pass 2E-A.2).

Every message is built from `redact.describe()` and redacted errors; the
command runs inside `redact.run_cli`, so even an unexpected exception leaves
as one redacted line.

Called by scripts/migrate.sh natively and by the `migrate` Compose service.
"""
from __future__ import annotations

import argparse
import sys

from . import migration_guard, schema_check
from .redact import describe, error_text

EXIT_WRITERS_ACTIVE = 6
EXIT_NOT_COORDINATED = 5
EXIT_FAILED = 7


def _alembic_config():
    from alembic.config import Config
    cfg = Config(str(schema_check.BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(schema_check.BACKEND / "alembic"))
    return cfg


def upgrade_on(connection) -> None:
    """`alembic upgrade head` on the given connection and no other."""
    from alembic import command
    cfg = _alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")
    connection.commit()


def dry_run(url: str) -> int:
    status = schema_check.check(url)
    print(status.message)
    if migration_guard.coordinated(url) and status.state != schema_check.UNREACHABLE:
        engine = conn = None
        try:
            engine, conn = migration_guard._connect(  # noqa: SLF001
                url, "quantdesk-migrate-dry-run", autocommit=True)
            found = migration_guard.holders(conn)
            print(f"writers holding the schema lock: {migration_guard._summary(found)}")  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            print(f"could not list writers: {error_text(exc, url)}")
        finally:
            migration_guard._close_quietly(conn, engine)  # noqa: SLF001
    if status.state in (schema_check.BEHIND, schema_check.UNVERSIONED):
        print("Dry run only. Stop every writer, back up the database, then apply.")
        return 0
    return status.exit_code


def apply(url: str, *, upgrade=upgrade_on) -> int:
    if not migration_guard.coordinated(url):
        print(f"Not migrating: {describe(url)} is not PostgreSQL; migrations are "
              "coordinated on PostgreSQL only", file=sys.stderr)
        return EXIT_NOT_COORDINATED
    try:
        with migration_guard.MigrationLock(url) as lock:
            conn = lock.connection
            before = schema_check.check_connection(conn, url)
            print(before.message)
            if before.ok:
                return 0
            if before.state not in (schema_check.BEHIND, schema_check.UNVERSIONED):
                print("Not migrating: resolve the above first.", file=sys.stderr)
                return before.exit_code
            try:
                upgrade(conn)
            except Exception as exc:  # noqa: BLE001
                print(f"migration failed on {describe(url)}: {error_text(exc, url)}",
                      file=sys.stderr)
                return EXIT_FAILED
            after = schema_check.check_connection(conn, url)
            print(after.message)
            return 0 if after.ok else EXIT_FAILED
    except migration_guard.WritersActive as exc:
        print(f"Not migrating: {exc}", file=sys.stderr)
        return EXIT_WRITERS_ACTIVE
    except migration_guard.CoordinationError as exc:
        print(f"Not migrating: {exc}", file=sys.stderr)
        return schema_check.EXIT_CODES[schema_check.UNREACHABLE]
    except Exception as exc:  # noqa: BLE001 — a revision read or cleanup failed
        print(f"migration aborted on {describe(url)}: {error_text(exc, url)}",
              file=sys.stderr)
        return EXIT_FAILED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending schema migrations.")
    parser.add_argument("--apply", action="store_true",
                        help="migrate (default: dry run)")
    args = parser.parse_args(argv)
    # Alembic logs to stderr unbuffered; keep this output in step with it.
    sys.stdout.reconfigure(line_buffering=True)
    url = migration_guard._default_url()  # noqa: SLF001
    return apply(url) if args.apply else dry_run(url)


if __name__ == "__main__":
    from .redact import run_cli
    sys.exit(run_cli(main))
