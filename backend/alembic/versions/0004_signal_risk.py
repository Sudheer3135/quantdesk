"""The risk decision each signal was published with.

`SignalRecord` says of itself that it is "what makes the system auditable
after the fact", but it stored no risk decision — so the one question a
governance record exists to answer, "did the desk approve this trade and
why", could not be answered from the database at all.

A column rather than a key inside `context`: that column holds the market
reading the signal engine produced, and the dashboard reads it as such.
Folding a governance record into it would leave neither column meaning one
thing.

Nullable, and no backfill. Rows written before this existed have no decision
to report, and inventing one — even a plausible one — would put a fabricated
governance record into the audit trail. NULL reads as "not recorded", which
is true.

Revision ID: 0004
Revises: 0003
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import add_column_if_absent

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_column_if_absent("signals", sa.Column("risk", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("risk")
