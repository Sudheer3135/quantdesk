"""Persist decision clocks and provenance; keep revisions; split option clocks.

Repair Pass 2C. Three additions, all nullable and none backfilled:

  signals          the six decision clocks as columns (TC-2) and what
                   produced the signal (RP-2). Existing rows keep NULL and
                   `clock_basis` NULL, which is how a legacy row is told apart
                   from a provenance-aware one. Their timestamps are not
                   guessed now and stored as if they had been observed.

  candle_revisions an append-only history of superseded candle values
                   (TC-6). Starts empty: the values earlier re-imports
                   overwrote are gone, and nothing here pretends otherwise.

  option_candles   exchange_time, capture_time and an immutable first_seen
                   (OC-1), plus depth at the touch (bid_size, ask_size).
                   Legacy rows keep NULL and are read under the old
                   bucket-close availability rule, labelled.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import (
    add_column_if_absent,
    create_index_if_absent,
    create_table_if_absent,
    drop_index_if_present,
)

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)

SIGNAL_COLUMNS = (
    sa.Column("bar_open_time", TZ, nullable=True),
    sa.Column("bar_close_time", TZ, nullable=True),
    sa.Column("available_at", TZ, nullable=True),
    sa.Column("received_at", TZ, nullable=True),
    sa.Column("decision_at", TZ, nullable=True),
    sa.Column("earliest_execution_time", TZ, nullable=True),
    sa.Column("clock_basis", sa.String(24), nullable=True),
    sa.Column("strategy_version", sa.String(32), nullable=True),
    sa.Column("parameter_hash", sa.String(64), nullable=True),
    sa.Column("input_fingerprint", sa.String(64), nullable=True),
    sa.Column("data_source", sa.String(64), nullable=True),
    sa.Column("code_id", sa.String(96), nullable=True),
    sa.Column("provenance", sa.JSON, nullable=True),
)

OPTION_COLUMNS = (
    sa.Column("bid_size", sa.Float, nullable=True),
    sa.Column("ask_size", sa.Float, nullable=True),
    sa.Column("exchange_time", TZ, nullable=True),
    sa.Column("capture_time", TZ, nullable=True),
    sa.Column("first_seen", TZ, nullable=True),
)


def upgrade() -> None:
    for column in SIGNAL_COLUMNS:
        add_column_if_absent("signals", column.copy())
    create_index_if_absent("ix_signals_clock_basis", "signals", ["clock_basis"])
    create_index_if_absent("ix_signals_parameter_hash", "signals", ["parameter_hash"])

    create_table_if_absent(
        "candle_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("timestamp", TZ, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("volume_is_synthetic", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("known_from", TZ, nullable=True),
        sa.Column("superseded_at", TZ, nullable=False),
    )
    create_index_if_absent("ix_candle_revision_bar", "candle_revisions",
                           ["symbol", "timeframe", "timestamp"])

    for column in OPTION_COLUMNS:
        add_column_if_absent("option_candles", column.copy())


def downgrade() -> None:
    with op.batch_alter_table("option_candles") as batch:
        for column in OPTION_COLUMNS:
            batch.drop_column(column.name)

    drop_index_if_present("ix_candle_revision_bar", "candle_revisions")
    op.drop_table("candle_revisions")

    drop_index_if_present("ix_signals_parameter_hash", "signals")
    drop_index_if_present("ix_signals_clock_basis", "signals")
    with op.batch_alter_table("signals") as batch:
        for column in SIGNAL_COLUMNS:
            batch.drop_column(column.name)
