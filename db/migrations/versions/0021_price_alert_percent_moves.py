"""price alerts: percent-move thresholds

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-23

Adds metadata for alerts whose absolute trigger price is derived from the
current price at creation time plus a requested percentage move.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_alerts",
        sa.Column(
            "alert_mode",
            sa.Text,
            nullable=False,
            server_default=sa.text("'price_threshold'"),
        ),
    )
    op.add_column("price_alerts", sa.Column("threshold_pct", sa.Numeric(8, 2)))
    op.add_column("price_alerts", sa.Column("baseline_price", sa.Numeric(12, 2)))
    op.add_column("price_alerts", sa.Column("baseline_source", sa.Text))
    op.create_check_constraint(
        "ck_price_alerts_alert_mode",
        "price_alerts",
        "alert_mode IN ('price_threshold', 'percent_move')",
    )
    op.create_check_constraint(
        "ck_price_alerts_threshold_pct",
        "price_alerts",
        "threshold_pct IS NULL OR threshold_pct > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_price_alerts_threshold_pct", "price_alerts")
    op.drop_constraint("ck_price_alerts_alert_mode", "price_alerts")
    op.drop_column("price_alerts", "baseline_source")
    op.drop_column("price_alerts", "baseline_price")
    op.drop_column("price_alerts", "threshold_pct")
    op.drop_column("price_alerts", "alert_mode")
