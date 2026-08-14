"""Idempotent DDL helpers for the migrations.

This platform predates its own migration chain. `init_db()` builds the
schema with `Base.metadata.create_all`, which means a database can arrive at
`alembic upgrade head` in three different states:

  - empty (a fresh install),
  - holding the *old* tables (a deployment that has been archiving candles
    for months), or
  - holding the *new* tables already, because the app booted and ran
    `create_all` before anyone ran a migration.

The third case is the awkward one. A migration that assumes it is creating
something from nothing fails there with "table already exists", leaving a
half-applied schema and an error that suggests no obvious next step.

Making each DDL step skip work that is already done costs a few lines and
removes an entire category of upgrade-day failure. The check is against the
live database, not against a guess about which state it is in.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

log = logging.getLogger("alembic.guards")


def _inspector():
    return sa.inspect(op.get_bind())


def table_exists(name: str) -> bool:
    return name in set(_inspector().get_table_names())


def create_table_if_absent(name: str, *columns) -> bool:
    """Create a table unless it is already there. True if it was created."""
    if table_exists(name):
        log.info("table %s already exists — skipping create", name)
        return False
    op.create_table(name, *columns)
    return True


def add_column_if_absent(table: str, column: sa.Column) -> bool:
    """Add a column unless it is already there. True if it was added."""
    existing = {c["name"] for c in _inspector().get_columns(table)}
    if column.name in existing:
        log.info("%s.%s already exists — skipping add", table, column.name)
        return False
    op.add_column(table, column)
    return True


def create_index_if_absent(name: str, table: str, columns: list[str]) -> bool:
    """Create an index unless one of that name is already there."""
    existing = {ix["name"] for ix in _inspector().get_indexes(table)}
    if name in existing:
        return False
    op.create_index(name, table, columns)
    return True


def drop_index_if_present(name: str, table: str) -> bool:
    existing = {ix["name"] for ix in _inspector().get_indexes(table)}
    if name not in existing:
        return False
    op.drop_index(name, table_name=table)
    return True
