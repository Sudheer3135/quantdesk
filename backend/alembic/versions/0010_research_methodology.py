"""An append-only research event log (Repair Pass 2D).

One table, `research_events`, holds every methodology record: which data
research has seen (OS-1), the prospective holdout lock and its consumption,
trial preregistrations and results (OS-4), and exposures of protected
sessions. Nothing is ever updated or deleted; each row carries the hash of
the row before it, so an edit anywhere breaks the chain from that point on
and `methodology.events.verify` reports where.

Starts empty. The sessions research has already used are not written here
by the migration: `methodology.registry` classifies every unregistered
session as SEEN_PRE_REGISTRY, which is the conservative reading of an
archive inspected before any registry existed.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import (
    create_index_if_absent,
    create_table_if_absent,
    drop_index_if_present,
)

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# Kept in step with `app.models.CONSUMPTION_RULE`; a migration must not
# import the model it builds, so the rule is written out in both places.
CONSUMPTION_RULE = (
    "(event_type = 'holdout_consumed' AND consumed_lock_id IS NOT NULL "
    "AND consumed_lock_id = subject) OR "
    "(event_type <> 'holdout_consumed' AND consumed_lock_id IS NULL)")


def upgrade() -> None:
    create_table_if_absent(
        "research_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("seq", sa.Integer, nullable=False, unique=True),
        sa.Column("stream", sa.String(24), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        # The holdout generation a `holdout_consumed` event spends. Required
        # on exactly those events (and equal to their subject), absent on
        # every other, and unique: the database itself admits at most one
        # consumption per generation — equivalent to UNIQUE(lock_id) WHERE
        # event_type = 'holdout_consumed', on SQLite and PostgreSQL alike,
        # whatever the application does.
        sa.Column("consumed_lock_id", sa.String(128), nullable=True, unique=True),
        sa.CheckConstraint(CONSUMPTION_RULE, name="ck_research_events_consumption"),
    )
    create_index_if_absent("ix_research_events_stream", "research_events",
                           ["stream", "subject"])


def downgrade() -> None:
    drop_index_if_present("ix_research_events_stream", "research_events")
    op.drop_table("research_events")
