"""Strategy v2 on paper: VIX history, paper positions, and its decisions.

Three new tables and no change to any existing one. v2 runs beside the desk
rather than inside it, so nothing it stores may alter what the signal
engine, the plan or the journal read.

Revision ID: 0007
Revises: 0006
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import (
    create_index_if_absent,
    create_table_if_absent,
    table_exists,
)

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_table_if_absent(
        "vix_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_date", name="uq_vix_daily"),
    )

    create_table_if_absent(
        "paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy", sa.String(16), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("contract", sa.String(48), nullable=False),
        sa.Column("token", sa.String(16), nullable=True),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("index_entry", sa.Float(), nullable=False),
        sa.Column("index_stop", sa.Float(), nullable=False),
        sa.Column("index_target", sa.Float(), nullable=False),
        sa.Column("premium_entry", sa.Float(), nullable=False),
        sa.Column("premium_stop", sa.Float(), nullable=False),
        sa.Column("premium_target", sa.Float(), nullable=False),
        sa.Column("risk_amount", sa.Float(), nullable=False),
        sa.Column("last_premium", sa.Float(), nullable=True),
        sa.Column("best_premium", sa.Float(), nullable=True),
        sa.Column("worst_premium", sa.Float(), nullable=True),
        sa.Column("premium_exit", sa.Float(), nullable=True),
        sa.Column("index_exit", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(24), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("costs", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("r_multiple", sa.Float(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    if table_exists("paper_positions"):
        for column in ("strategy", "status", "session_date"):
            create_index_if_absent(f"ix_paper_positions_{column}",
                                   "paper_positions", [column])

    create_table_if_absent(
        "paper_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy", sa.String(16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("signal_time", sa.String(40), nullable=True),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("outcome", sa.String(8), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    if table_exists("paper_decisions"):
        for column in ("strategy", "session_date", "outcome", "code"):
            create_index_if_absent(f"ix_paper_decisions_{column}",
                                   "paper_decisions", [column])


def downgrade() -> None:
    op.drop_table("paper_decisions")
    op.drop_table("paper_positions")
    op.drop_table("vix_daily")
