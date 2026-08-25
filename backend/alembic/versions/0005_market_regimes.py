"""Market regime history, one row per bar.

Split from `candles` rather than added as columns on it. A candle is an
observation and never changes; a regime is an interpretation and will change
the first time a threshold is argued with. Putting a mutable derived label on
an immutable observation row would mean rewriting the archive to re-classify
it, and `revision` on `candles` exists to flag restated *prices* — a bump
there for a regime change would be a false alarm about the data itself.

Two levels on one row because they describe the same bar and are always read
together. `engine_version` is not decoration: a table holding two generations
of the classifier would produce a regime split that looks like a finding and
is an artefact of the mix.

Revision ID: 0005
Revises: 0004
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.data.migration_guards import (
    create_index_if_absent,
    create_table_if_absent,
    table_exists,
)

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    created = create_table_if_absent(
        "market_regimes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("day_regime", sa.String(16), nullable=False),
        sa.Column("day_confidence", sa.Float(), nullable=False),
        sa.Column("day_reasons", sa.JSON(), nullable=True),
        sa.Column("hour_regime", sa.String(16), nullable=False),
        sa.Column("hour_confidence", sa.Float(), nullable=False),
        sa.Column("hour_reasons", sa.JSON(), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("engine_version", sa.String(16), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("symbol", "timeframe", "timestamp",
                            name="uq_market_regime"),
    )

    # Guarded individually rather than inside the `created` branch: a
    # database that already had the table from `create_all` still needs the
    # indexes checked, and every helper here is a no-op when the object is
    # already present.
    if created or table_exists("market_regimes"):
        create_index_if_absent("ix_market_regimes_symbol", "market_regimes", ["symbol"])
        create_index_if_absent("ix_market_regimes_timeframe", "market_regimes",
                               ["timeframe"])
        create_index_if_absent("ix_market_regimes_timestamp", "market_regimes",
                               ["timestamp"])
        create_index_if_absent("ix_market_regimes_session_date", "market_regimes",
                               ["session_date"])
        create_index_if_absent("ix_market_regimes_day_regime", "market_regimes",
                               ["day_regime"])
        create_index_if_absent("ix_market_regimes_hour_regime", "market_regimes",
                               ["hour_regime"])
        create_index_if_absent("ix_regime_session", "market_regimes",
                               ["symbol", "timeframe", "session_date"])


def downgrade() -> None:
    op.drop_table("market_regimes")
