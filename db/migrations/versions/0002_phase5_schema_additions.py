"""phase 5 schema additions: insights.text_value, sources.denomination

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Daily narrative insights (insight_type='daily_narrative') need to
    # store an English paragraph. NUMERIC `value` is wrong type, JSONB
    # meta_info hurts FTS/indexing — dedicated TEXT column per ADR 007.
    op.add_column(
        "insights",
        sa.Column("text_value", sa.Text, nullable=True),
    )

    # Each source has its own pricing denomination — Steam Market quotes
    # in Steam Wallet credit (~30-50% structural premium vs USD because
    # wallet can't be withdrawn to real money); Skinport quotes in USD
    # (real-money payout). Analytics + narrative must label prices
    # accordingly so the bot doesn't surface "$42 vs $28" without
    # context. See docs/sources-and-semantics.md for the full
    # discussion. TEXT (not enum) so adding 'rmb' / 'eth' / etc. later
    # is a row update, not a schema change.
    op.add_column(
        "sources",
        sa.Column("denomination", sa.Text, nullable=True),
    )

    # Backfill known sources. Future sources are populated by the seed
    # script from data/watchlist.yaml.
    op.execute(
        "UPDATE sources SET denomination = 'wallet_credit' "
        "WHERE name = 'steam_market'"
    )
    op.execute(
        "UPDATE sources SET denomination = 'usd' WHERE name = 'skinport'"
    )


def downgrade() -> None:
    op.drop_column("sources", "denomination")
    op.drop_column("insights", "text_value")
