"""price alerts: delivery acknowledgement and retry state

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-23

Adds delivery bookkeeping so triggered price alerts are not considered complete
until Discord delivery is acknowledged by the bot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_alerts",
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "price_alerts",
        sa.Column(
            "delivery_attempts",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("price_alerts", sa.Column("last_delivery_error", sa.Text))
    op.create_index(
        "ix_price_alerts_pending_delivery",
        "price_alerts",
        ["status", "delivered_at", "created_at"],
        postgresql_where=sa.text("status = 'triggered' AND delivered_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_price_alerts_pending_delivery", table_name="price_alerts")
    op.drop_column("price_alerts", "last_delivery_error")
    op.drop_column("price_alerts", "delivery_attempts")
    op.drop_column("price_alerts", "delivered_at")
