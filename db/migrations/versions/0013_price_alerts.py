"""price alerts: persistent Discord alert subscriptions

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-23

Adds persistent price-alert subscriptions owned by Discord user/channel ids.
The bot creates these records through the read API; a delivery loop can evaluate
active rows and mark them triggered without exposing write access to the LLM.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_alerts",
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
        sa.Column("discord_channel_id", sa.Text),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug_snapshot", sa.Text, nullable=False),
        sa.Column("display_name_snapshot", sa.Text, nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("threshold_price", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("triggered_at", sa.DateTime(timezone=True)),
        sa.Column("trigger_price", sa.Numeric(12, 2)),
        sa.Column("trigger_source", sa.Text),
        sa.CheckConstraint(
            "currency IN ('usd', 'wallet_credit')",
            name="ck_price_alerts_currency",
        ),
        sa.CheckConstraint(
            "direction IN ('at_or_below', 'at_or_above')",
            name="ck_price_alerts_direction",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'triggered', 'cancelled')",
            name="ck_price_alerts_status",
        ),
    )
    op.create_index(
        "ix_price_alerts_user_status_created",
        "price_alerts",
        ["discord_user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_price_alerts_active_item",
        "price_alerts",
        ["item_id"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_price_alerts_active_item", table_name="price_alerts")
    op.drop_index("ix_price_alerts_user_status_created", table_name="price_alerts")
    op.drop_table("price_alerts")
