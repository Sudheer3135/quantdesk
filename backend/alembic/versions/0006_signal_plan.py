"""The two-layer read stored with each signal: bias and entry state.

Two indexed string columns plus a JSON body, matching how `risk` is stored
and for the same reason. `bias` and `entry_state` are what a study filters
and groups by — "what did the desk do when it said BULLISH but WAIT_PULLBACK"
is a query, not a scan — while the confidences, the individual
higher-timeframe readings and the sentences justifying them belong in one
blob nobody indexes.

Nullable, and no backfill. Rows written before this existed carry no bias and
no entry state. Computing one now from today's code and writing it into a
historical row would put a fabricated record into an audit trail whose whole
purpose is to say what the desk actually thought at the time. The replay in
`evaluation.two_layer` recomputes them deliberately, holds them in memory,
and labels the result a study rather than a record.

Revision ID: 0006
Revises: 0005
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import (
    add_column_if_absent,
    create_index_if_absent,
    drop_index_if_present,
)

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_column_if_absent("signals", sa.Column("bias", sa.String(8), nullable=True))
    add_column_if_absent("signals",
                         sa.Column("entry_state", sa.String(16), nullable=True))
    add_column_if_absent("signals", sa.Column("plan", sa.JSON(), nullable=True))
    create_index_if_absent("ix_signals_bias", "signals", ["bias"])
    create_index_if_absent("ix_signals_entry_state", "signals", ["entry_state"])


def downgrade() -> None:
    # Indexes first, and outside the batch block. SQLite has no DROP COLUMN,
    # so alembic's batch mode rebuilds the table and copies the old one's
    # indexes across — including the ones on the columns being dropped, which
    # then fails with "no such column: bias" on a rebuild that looks like it
    # should have worked.
    drop_index_if_present("ix_signals_entry_state", "signals")
    drop_index_if_present("ix_signals_bias", "signals")
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("plan")
        batch.drop_column("entry_state")
        batch.drop_column("bias")
