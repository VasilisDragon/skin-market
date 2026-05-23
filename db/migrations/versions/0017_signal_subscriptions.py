"""signal subscriptions: recurring Discord digest delivery

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-23

Adds persisted Discord signal digest subscriptions with quiet-hour and delivery
state. The bot evaluates due rows through the API and acknowledges delivery
after Discord sends the digest message.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_subscriptions",
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
        sa.Column("hours", sa.Integer, nullable=False),
        sa.Column("limit", sa.Integer, nullable=False),
        sa.Column("threshold_z", sa.Numeric(6, 2), nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
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
        sa.Column("last_delivery_attempt_at", sa.DateTime(timezone=True)),
        sa.Column(
            "delivery_attempts",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_delivery_error", sa.Text),
        sa.Column("last_digest_fingerprint", sa.Text),
        sa.CheckConstraint(
            "hours >= 1 AND hours <= 24",
            name="ck_signal_subscriptions_hours",
        ),
        sa.CheckConstraint(
            "\"limit\" >= 1 AND \"limit\" <= 20",
            name="ck_signal_subscriptions_limit",
        ),
        sa.CheckConstraint(
            "threshold_z >= 0",
            name="ck_signal_subscriptions_threshold_z",
        ),
        sa.CheckConstraint(
            "interval_minutes >= 15 AND interval_minutes <= 10080",
            name="ck_signal_subscriptions_interval",
        ),
        sa.CheckConstraint(
            "quiet_start_hour IS NULL OR "
            "(quiet_start_hour >= 0 AND quiet_start_hour <= 23)",
            name="ck_signal_subscriptions_quiet_start",
        ),
        sa.CheckConstraint(
            "quiet_end_hour IS NULL OR "
            "(quiet_end_hour >= 0 AND quiet_end_hour <= 23)",
            name="ck_signal_subscriptions_quiet_end",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'cancelled')",
            name="ck_signal_subscriptions_status",
        ),
    )
    op.create_index(
        "ix_signal_subscriptions_user_status_created",
        "signal_subscriptions",
        ["discord_user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_signal_subscriptions_active_delivery",
        "signal_subscriptions",
        ["status", "last_sent_at", "last_delivery_attempt_at"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_signal_subscriptions_active_delivery",
        table_name="signal_subscriptions",
    )
    op.drop_index(
        "ix_signal_subscriptions_user_status_created",
        table_name="signal_subscriptions",
    )
    op.drop_table("signal_subscriptions")
