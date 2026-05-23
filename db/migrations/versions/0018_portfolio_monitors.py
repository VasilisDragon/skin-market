"""portfolio monitors: recurring baseline change notifications

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-23

Adds recurring public-inventory portfolio monitors. The API periodically saves a
summary-level portfolio snapshot and returns a Discord delivery payload when the
baseline moves beyond the configured threshold.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_monitors",
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
        sa.Column("discord_channel_id", sa.Text, nullable=False),
        sa.Column("inventory_url", sa.Text, nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        sa.Column("change_threshold_pct", sa.Numeric(8, 2), nullable=False),
        sa.Column("quiet_start_hour", sa.Integer),
        sa.Column("quiet_end_hour", sa.Integer),
        sa.Column(
            "timezone_offset_minutes",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_snapshot_id", UUID(as_uuid=True)),
        sa.Column("last_delivery_attempt_at", sa.DateTime(timezone=True)),
        sa.Column(
            "delivery_attempts",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_delivery_error", sa.Text),
        sa.CheckConstraint(
            "interval_minutes >= 60 AND interval_minutes <= 10080",
            name="ck_portfolio_monitors_interval",
        ),
        sa.CheckConstraint(
            "change_threshold_pct >= 0",
            name="ck_portfolio_monitors_threshold",
        ),
        sa.CheckConstraint(
            "quiet_start_hour IS NULL OR "
            "(quiet_start_hour >= 0 AND quiet_start_hour <= 23)",
            name="ck_portfolio_monitors_quiet_start",
        ),
        sa.CheckConstraint(
            "quiet_end_hour IS NULL OR "
            "(quiet_end_hour >= 0 AND quiet_end_hour <= 23)",
            name="ck_portfolio_monitors_quiet_end",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'cancelled')",
            name="ck_portfolio_monitors_status",
        ),
    )
    op.create_index(
        "ix_portfolio_monitors_user_status_created",
        "portfolio_monitors",
        ["discord_user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_portfolio_monitors_active_delivery",
        "portfolio_monitors",
        ["status", "last_checked_at", "last_sent_at"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_monitors_active_delivery",
        table_name="portfolio_monitors",
    )
    op.drop_index(
        "ix_portfolio_monitors_user_status_created",
        table_name="portfolio_monitors",
    )
    op.drop_table("portfolio_monitors")
