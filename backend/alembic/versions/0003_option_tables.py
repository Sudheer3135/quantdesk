"""Option contracts, option candles, and dataset versions.

These tables start empty and stay empty until the agent has been running.
There is no free source of historical NIFTY option data — NSE publishes a
live snapshot, not a tape — so option history accumulates forward from the
day this ships and cannot be backfilled.

Revision ID: 0003
Revises: 0002
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import create_index_if_absent, create_table_if_absent

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_table_if_absent(
        "option_contracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("tradingsymbol", sa.String(64), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.UniqueConstraint("underlying", "expiry_date", "strike", "option_type",
                            name="uq_option_contract"),
    )
    create_index_if_absent("ix_option_contracts_underlying", "option_contracts", ["underlying"])
    create_index_if_absent("ix_option_contracts_expiry_date", "option_contracts", ["expiry_date"])
    create_index_if_absent("ix_option_contracts_strike", "option_contracts", ["strike"])

    create_table_if_absent(
        "option_candles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contract_id", sa.Integer(),
                  sa.ForeignKey("option_contracts.id"), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.Column("oi_change", sa.Float(), nullable=True),
        sa.Column("iv", sa.Float(), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("underlying_close", sa.Float(), nullable=True),
        sa.Column("bar_kind", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("samples", sa.Integer(), nullable=True),
        sa.UniqueConstraint("contract_id", "timeframe", "timestamp",
                            name="uq_option_candle"),
    )
    create_index_if_absent("ix_option_candles_contract_id", "option_candles", ["contract_id"])
    create_index_if_absent("ix_option_candles_timestamp", "option_candles", ["timestamp"])
    create_index_if_absent("ix_option_candles_session_date", "option_candles", ["session_date"])
    create_index_if_absent("ix_option_candle_time", "option_candles", ["timeframe", "timestamp"])

    create_table_if_absent(
        "dataset_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("first_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("session_count", sa.Integer(), nullable=False),
        sa.Column("source_mix", sa.JSON(), nullable=True),
        sa.Column("volume_is_synthetic", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    create_index_if_absent("ix_dataset_versions_hash", "dataset_versions", ["hash"])
    create_index_if_absent("ix_dataset_versions_symbol", "dataset_versions", ["symbol"])


def downgrade() -> None:
    op.drop_table("dataset_versions")
    op.drop_table("option_candles")
    op.drop_table("option_contracts")
