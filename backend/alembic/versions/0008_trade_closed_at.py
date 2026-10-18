"""Record journal exit times for daily realised risk.

Existing close times are unknown and remain NULL, rather than being
backfilled with the entry date or the migration date.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import add_column_if_absent, create_index_if_absent

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_absent("trades", sa.Column("closed_at", sa.DateTime(timezone=True),
                                           nullable=True))
    create_index_if_absent("ix_trades_closed_at", "trades", ["closed_at"])


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch:
        batch.drop_index("ix_trades_closed_at")
        batch.drop_column("closed_at")
