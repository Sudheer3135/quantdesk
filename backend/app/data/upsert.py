"""Idempotent inserts that work on both Postgres and SQLite.

The archiver used to import `pg_insert` directly, which made every write
path in the platform untestable: CI runs against `sqlite:///./test.db`, so
any test that touched a real insert failed on a dialect error before it
could assert anything. The write side of the system was therefore the least
covered part of it, which is precisely backwards for something whose whole
job is to not corrupt a dataset.

Both dialects implement the same `ON CONFLICT (cols) DO UPDATE` upsert. The
only difference is which module the `insert()` construct comes from, so that
is the only thing this module abstracts.

Two behaviours here are not obvious and both were learned the hard way:

  1. **De-duplicate inside the batch before sending it.** Postgres raises
     "ON CONFLICT DO UPDATE command cannot affect row a second time" if the
     same key appears twice in one statement. Yahoo occasionally repeats a
     timestamp across a session boundary, so relying on the conflict clause
     alone is not enough.

  2. **Chunk the batch.** Postgres caps a statement at 65535 bind
     parameters. A 59-day backfill of 5-minute candles at a dozen columns
     each is close enough to that ceiling to matter, and one enormous
     VALUES clause is slow to compile besides.
"""
from __future__ import annotations

import logging
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Rows per statement. Conservative enough to stay well inside Postgres's
# bind-parameter ceiling even for the widest table here (option_candles).
CHUNK_SIZE = 500


@dataclass
class UpsertResult:
    """What a write actually did.

    `inserted` and `updated` are reported separately because they mean very
    different things: a re-import that reports thousands of updates is
    restating history, and you want to know that rather than see one
    reassuring "written" total.
    """
    seen: int = 0             # rows handed to us
    deduplicated: int = 0     # dropped as in-batch duplicates
    inserted: int = 0         # new rows
    updated: int = 0          # existing rows overwritten
    failed: int = 0           # rows in chunks that raised

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def to_dict(self) -> dict:
        return {
            "seen": self.seen,
            "deduplicated": self.deduplicated,
            "inserted": self.inserted,
            "updated": self.updated,
            "written": self.written,
            "failed": self.failed,
        }


def _insert_for(db: Session):
    """The dialect's insert construct, or a clear error naming the dialect."""
    name = db.get_bind().dialect.name
    if name == "postgresql":
        return pg_insert
    if name == "sqlite":
        return sqlite_insert
    raise NotImplementedError(
        f"upsert is implemented for postgresql and sqlite, not {name!r}. "
        "Add the dialect here rather than falling back to a read-then-write, "
        "which is not atomic and will duplicate rows under concurrency."
    )


def dedupe(rows: Sequence[dict], conflict_columns: Sequence[str]) -> tuple[list[dict], int]:
    """Collapse rows sharing a conflict key. The last one wins.

    Last-wins is deliberate: when a source hands back the same bar twice in
    one payload, the later copy is the fresher read.
    """
    keyed: dict[tuple[Hashable, ...], dict] = {}
    for row in rows:
        keyed[tuple(row[c] for c in conflict_columns)] = row
    return list(keyed.values()), len(rows) - len(keyed)


def upsert(
    db: Session,
    model: Any,
    rows: Sequence[dict],
    *,
    conflict_columns: Sequence[str],
    update_columns: Sequence[str],
    extra_set: dict | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> UpsertResult:
    """Insert rows, overwriting any that already exist. Idempotent.

    `conflict_columns` must be covered by a unique constraint or index —
    both dialects infer the target from the column list rather than the
    constraint name, which is the one spelling they agree on.

    `extra_set` holds update expressions that are not a straight copy from
    the incoming row, such as bumping a revision counter. A value may be a
    plain SQL expression, or a callable taking the statement's `excluded`
    pseudo-table — the latter is how a merge like "keep the higher of the
    stored and incoming high" is expressed, since `excluded` does not exist
    until the statement is built in here.

    Counting note: inserted-versus-updated is derived from the table's row
    count either side of the write. That is exact for a single writer, which
    is what the importer is. It would undercount inserts if another process
    were writing the same table concurrently, and the alternative —
    Postgres's `RETURNING (xmax = 0)` — has no SQLite equivalent, so the
    portable answer wins here.
    """
    result = UpsertResult(seen=len(rows))
    if not rows:
        return result

    batch, result.deduplicated = dedupe(rows, conflict_columns)
    if result.deduplicated:
        log.info("dropped %s duplicate keys before insert", result.deduplicated)

    insert = _insert_for(db)
    before = db.scalar(select(func.count()).select_from(model))

    written = 0
    for start in range(0, len(batch), chunk_size):
        chunk = batch[start : start + chunk_size]
        stmt = insert(model).values(chunk)
        set_ = {c: getattr(stmt.excluded, c) for c in update_columns}
        for column, value in (extra_set or {}).items():
            set_[column] = value(stmt.excluded) if callable(value) else value
        stmt = stmt.on_conflict_do_update(index_elements=list(conflict_columns), set_=set_)
        try:
            db.execute(stmt)
            db.commit()
            written += len(chunk)
        except Exception:
            # Commit per chunk so one bad batch does not discard the rest of
            # a long backfill. The failure is counted, not swallowed.
            db.rollback()
            result.failed += len(chunk)
            log.exception("upsert chunk starting at row %s failed; continuing", start)

    after = db.scalar(select(func.count()).select_from(model))
    result.inserted = max(0, (after or 0) - (before or 0))
    result.updated = max(0, written - result.inserted)

    # These are Core statements, so the session's identity map never sees
    # them. Any ORM instance loaded before this call would keep serving its
    # pre-upsert values from memory — the row changes in the database and
    # the object in front of you does not. Expiring forces a reload on next
    # access, which is the behaviour every caller assumes it already has.
    db.expire_all()
    return result
