"""portfolio snapshots: persisted Discord inventory baselines

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-23

Stores summary-level public-inventory baseline snapshots owned by Discord user
ids. The table intentionally keeps portfolio totals and bounded item samples,
not full raw inventories, so trend queries can work without retaining every
asset from a user's Steam inventory.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("discord_user_id", sa.Text, nullable=False),
        sa.Column("steam_id", sa.Text, nullable=False),
        sa.Column("inventory_url", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("currency", sa.Text),
        sa.Column("baseline_low", sa.Numeric(14, 2)),
        sa.Column("baseline_mid", sa.Numeric(14, 2)),
        sa.Column("baseline_high", sa.Numeric(14, 2)),
        sa.Column("priced_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unpriced_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stickered_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("top_item_share_pct", sa.Numeric(8, 2)),
        sa.Column("portfolio_baseline", JSONB),
        sa.Column(
            "top_items",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "largest_spread_items",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "unpriced_sample",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'no_value_data')",
            name="ck_portfolio_snapshots_status",
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency IN ('usd')",
            name="ck_portfolio_snapshots_currency",
        ),
    )
    op.create_index(
        "ix_portfolio_snapshots_user_created",
        "portfolio_snapshots",
        ["discord_user_id", "created_at"],
    )
    op.create_index(
        "ix_portfolio_snapshots_user_steam_created",
        "portfolio_snapshots",
        ["discord_user_id", "steam_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshots_user_steam_created",
        table_name="portfolio_snapshots",
    )
    op.drop_index(
        "ix_portfolio_snapshots_user_created",
        table_name="portfolio_snapshots",
    )
    op.drop_table("portfolio_snapshots")
