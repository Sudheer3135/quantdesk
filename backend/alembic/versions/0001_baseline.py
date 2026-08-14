"""Baseline — the schema as it stood before the historical data pipeline.

This revision has to cope with two different databases: a fresh install
where it creates everything, and a running install that already holds
months of archived candles created by `Base.metadata.create_all`.

The usual answer for the second case is `alembic stamp 0001`, which means
anyone upgrading has to know that, remember it, and get it right before the
first `upgrade head` — and if they don't, the error they see is a confusing
"table already exists". So this revision skips tables that are already
there instead. `alembic upgrade head` then does the right thing on both,
and nobody's accumulated history depends on them having read a release note.

Revision ID: 0001
Revises:
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import create_table_if_absent

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_table_if_absent(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("symbol", sa.String(32), index=True),
        sa.Column("timeframe", sa.String(8)),
        sa.Column("action", sa.String(8), index=True),
        sa.Column("confidence", sa.Float()),
        sa.Column("price", sa.Float()),
        sa.Column("entry", sa.Float(), nullable=True),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("checks", sa.JSON()),
        sa.Column("context", sa.JSON()),
    )

    create_table_if_absent(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("symbol", sa.String(48), index=True),
        sa.Column("side", sa.String(8)),
        sa.Column("quantity", sa.Integer()),
        sa.Column("entry", sa.Float()),
        sa.Column("exit", sa.Float(), nullable=True),
        sa.Column("stop_loss", sa.Float()),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("r_multiple", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), index=True),
        sa.Column("signal_id", sa.Integer(), nullable=True),
        sa.Column("setup", sa.String(64), nullable=True),
        sa.Column("plan_followed", sa.Boolean(), nullable=True),
        sa.Column("mistakes", sa.Text(), nullable=True),
        sa.Column("lesson", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
    )

    create_table_if_absent(
        "candles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), index=True),
        sa.Column("timeframe", sa.String(8), index=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), index=True),
        sa.Column("open", sa.Float()),
        sa.Column("high", sa.Float()),
        sa.Column("low", sa.Float()),
        sa.Column("close", sa.Float()),
        sa.Column("volume", sa.Float()),
        sa.Column("source", sa.String(16), nullable=True),
        sa.UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle"),
    )


def downgrade() -> None:
    op.drop_table("candles")
    op.drop_table("trades")
    op.drop_table("signals")
