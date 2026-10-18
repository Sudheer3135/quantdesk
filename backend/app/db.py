"""Database session handling."""
import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .migration_guard import protect_writes
from .models import Base

log = logging.getLogger(__name__)

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
# Every write transaction on this engine — every Session, flush and execute
# of every supported writer — takes the shared schema lock on its own
# connection before its first write, and holds it to commit or rollback. A
# migration in progress refuses the write before it is sent (Pass 2E-A.2).
protect_writes(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Make sure the schema exists, without fighting the migration chain.

    Alembic owns the schema now. `create_all` stays for two cases it is
    genuinely better at — a throwaway SQLite database in the tests, and a
    fresh local run where nobody has invoked alembic yet — and steps aside
    the moment a version table exists.

    The middle case is the one worth the warning: tables present, no version
    table, means a database built by an older release. It must be migrated,
    and `create_all` will not do it — it silently skips tables that already
    exist, so the new provenance columns would never appear and every write
    would fail at runtime with nothing explaining why.
    """
    tables = set(inspect(engine).get_table_names())

    if "alembic_version" in tables:
        return

    if tables:
        log.warning(
            "Database has tables but no alembic_version — it predates the "
            "migration chain. Run `alembic upgrade head` from backend/ "
            "before using it; create_all cannot add columns to tables that "
            "already exist, so the historical pipeline will fail on write "
            "until you do."
        )
        return

    Base.metadata.create_all(engine)
    log.info("Created schema with create_all. For anything but a scratch "
             "database, prefer `alembic upgrade head`.")


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
