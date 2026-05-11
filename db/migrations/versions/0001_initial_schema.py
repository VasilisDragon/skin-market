"""initial schema: items, sources, prices hypertable, insights

Revision ID: 0001
Revises:
Create Date: 2026-05-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # TimescaleDB is shipped pre-loaded by the timescale/timescaledb image,
    # but we create the extension explicitly so this migration is portable
    # to other TimescaleDB-enabled Postgres clusters.
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "items",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("market_hash_name", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("item_type", sa.Text),
        sa.Column("weapon_name", sa.Text),
        sa.Column("skin_name", sa.Text),
        sa.Column("wear", sa.Text),
        sa.Column(
            "is_stattrak", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "is_souvenir", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("base_url", sa.Text),
        sa.Column("rate_limit_per_minute", sa.Integer),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
    )

    op.create_table(
        "prices",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id"),
            primary_key=True,
        ),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("sources.id"),
            primary_key=True,
        ),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), primary_key=True
        ),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("volume", sa.Integer),
        sa.Column(
            "currency", sa.String(8), nullable=False, server_default="USD"
        ),
        sa.Column("raw_response", JSONB),
    )

    # Convert ``prices`` to a TimescaleDB hypertable. Weekly chunks at the
    # v1 write rate (~17k rows/day) produce ~120k-row chunks — comfortable
    # for in-memory operations on a single chunk.
    op.execute(
        "SELECT create_hypertable('prices', 'timestamp', "
        "chunk_time_interval => INTERVAL '7 days')"
    )

    # Enable native columnar compression. Segmenting by (item_id, source_id)
    # means each compressed segment is a single time-series, which is what
    # TimescaleDB's columnar format excels at compressing.
    op.execute(
        """
        ALTER TABLE prices SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'item_id, source_id'
        )
        """
    )
    # Compress chunks older than 30 days. raw_response JSONB is the dominant
    # disk-cost component at v1 volumes (~100MB/mo uncompressed); expect
    # 10-20x compression on time-series chunks segmented by series.
    op.execute("SELECT add_compression_policy('prices', INTERVAL '30 days')")

    op.create_table(
        "insights",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id"),
            nullable=False,
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("insight_type", sa.Text, nullable=False),
        sa.Column("value", sa.Numeric),
        sa.Column("metadata", JSONB),
    )

    # Common query: "latest insight of type X for item Y". DESC index
    # answers it with a single index seek.
    op.execute(
        "CREATE INDEX ix_insights_item_type_computed_desc "
        "ON insights (item_id, insight_type, computed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_insights_item_type_computed_desc")
    op.drop_table("insights")
    # Dropping a hypertable also drops its associated policies and chunks.
    op.execute("DROP TABLE IF EXISTS prices CASCADE")
    op.drop_table("sources")
    op.drop_table("items")
    op.execute("DROP EXTENSION IF EXISTS timescaledb")
