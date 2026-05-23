"""signal subscriptions: named digest lanes

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-23

Adds a persisted lane selector so Discord channels can subscribe to targeted
signal feeds instead of only the broad all-signals digest.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signal_subscriptions",
        sa.Column(
            "lane",
            sa.Text,
            nullable=False,
            server_default=sa.text("'all'"),
        ),
    )
    op.create_check_constraint(
        "ck_signal_subscriptions_lane",
        "signal_subscriptions",
        "lane IN ('all', 'market_movers', 'spread_watch')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_signal_subscriptions_lane", "signal_subscriptions")
    op.drop_column("signal_subscriptions", "lane")
