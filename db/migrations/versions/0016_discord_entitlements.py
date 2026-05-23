"""discord entitlements: tier and quota policy roots

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-23

Adds operator-managed Discord entitlement rows. This is intentionally billing
agnostic: payment processors create business facts outside this repository; the
bot reads tier/status and applies deterministic quotas.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_entitlements",
        sa.Column("discord_user_id", sa.Text, primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.CheckConstraint(
            "tier IN ('free', 'trader', 'pro')",
            name="ck_discord_entitlements_tier",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_discord_entitlements_status",
        ),
    )
    op.create_index(
        "ix_discord_entitlements_status_tier",
        "discord_entitlements",
        ["status", "tier"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discord_entitlements_status_tier",
        table_name="discord_entitlements",
    )
    op.drop_table("discord_entitlements")
