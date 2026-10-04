"""What the connected database actually has, for readers of older archives.

Research reads frozen archives as well as the live database — the audit
snapshot is opened read-only and cannot be migrated. An ORM query for a
column that archive never had fails outright, which would make every
provenance column added since unreadable history in the one place history
matters most.

So readers ask here first, load only what exists, and label what is absent.
A missing column is reported as unavailable; it is never defaulted, because
a default would read exactly like an observed value.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session, load_only


def columns(db: Session, table: str) -> frozenset[str] | None:
    """Column names of `table` in this database, or None if it is absent.

    Inspected on every call rather than cached. One catalogue query per
    study is nothing, and a cache keyed on the engine could outlive a schema
    change — a test that migrates in place, or an engine object recycled
    under a new database — and then answer for the wrong one.
    """
    # The session's own connection, not the engine. Inspecting through the
    # engine checks a connection out of the pool and returns it with a
    # reset — a ROLLBACK — and where the pool hands back the session's own
    # connection (StaticPool, a single-connection setup) that silently
    # discarded the caller's flushed, uncommitted rows.
    inspector = sa.inspect(db.connection())
    if not inspector.has_table(table):
        return None
    return frozenset(c["name"] for c in inspector.get_columns(table))


def has_table(db: Session, table: str) -> bool:
    return columns(db, table) is not None


def has_columns(db: Session, table: str, *names: str) -> bool:
    present = columns(db, table)
    return present is not None and all(n in present for n in names)


def loadable(db: Session, model) -> tuple[list, set[str]]:
    """Load options naming exactly the model's columns this database has.

    Returns (options, missing column names). The option is always a
    `load_only` over the present columns, which also loads the deferred ones
    among them in the same query — so a migrated database pays no extra round
    trip per row, and an unmigrated one is never asked for a column it lacks.
    """
    present = columns(db, model.__tablename__) or frozenset()
    mapped = list(model.__table__.columns)
    missing = {c.name for c in mapped if c.name not in present}
    keep = [getattr(model, c.key) for c in mapped
            if c.name in present and not c.primary_key]
    return [load_only(*keep)], missing
