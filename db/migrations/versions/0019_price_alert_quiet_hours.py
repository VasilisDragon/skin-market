"""price alerts: quiet-hour delivery windows

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-23

Adds optional quiet-hour fields to threshold price alerts. Triggered alerts can
wait in pending-delivery state until the user's local quiet window ends.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("price_alerts", sa.Column("quiet_start_hour", sa.Integer))
    op.add_column("price_alerts", sa.Column("quiet_end_hour", sa.Integer))
    op.add_column(
        "price_alerts",
        sa.Column(
            "timezone_offset_minutes",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_price_alerts_quiet_start",
        "price_alerts",
        "quiet_start_hour IS NULL OR "
        "(quiet_start_hour >= 0 AND quiet_start_hour <= 23)",
    )
    op.create_check_constraint(
        "ck_price_alerts_quiet_end",
        "price_alerts",
        "quiet_end_hour IS NULL OR "
        "(quiet_end_hour >= 0 AND quiet_end_hour <= 23)",
    )
    op.create_check_constraint(
        "ck_price_alerts_timezone_offset",
        "price_alerts",
        "timezone_offset_minutes >= -720 AND timezone_offset_minutes <= 840",
    )


def downgrade() -> None:
    op.drop_constraint("ck_price_alerts_timezone_offset", "price_alerts")
    op.drop_constraint("ck_price_alerts_quiet_end", "price_alerts")
    op.drop_constraint("ck_price_alerts_quiet_start", "price_alerts")
    op.drop_column("price_alerts", "timezone_offset_minutes")
    op.drop_column("price_alerts", "quiet_end_hour")
    op.drop_column("price_alerts", "quiet_start_hour")
