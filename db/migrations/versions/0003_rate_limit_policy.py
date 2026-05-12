"""rate-limit policy: per-source interval + delay columns on sources

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12

Adds ``sources.interval_minutes`` and ``sources.per_item_delay_seconds``
so the scheduler can iterate ``sources WHERE enabled = TRUE`` and pick
up each source's cadence from the DB instead of from hardcoded
constants. See ADR 013.

Initial values match the post-degradation policy:

- ``steam_market``:   60 min interval, 5s per-item delay (was 30/5;
                      halves request volume after observed degradation)
- ``skinport``:       15 min interval, 0s per-item delay (was 5/0;
                      bulk-fetch API so per-item delay is N/A; cadence
                      conservative since the IP just unbanned).
                      ``enabled`` is left untouched — operator step
                      flips it back to true after verifying the fix.
- ``dmarket``:        15 min interval, 3s per-item delay (current
                      cadence, healthy at this rate)

Any source not yet in the table picks up the column defaults via the
two-step add-then-backfill-then-NOT NULL pattern.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add columns with server defaults so existing rows pick up sensible
    # values immediately (30min / 5s — conservative cadence), then the
    # known-sources backfill overrides with the post-degradation policy.
    # Server defaults persist on the column so future inserts that don't
    # know about these columns (seed_watchlist, ad-hoc tests) still
    # produce valid rows.
    op.add_column(
        "sources",
        sa.Column(
            "interval_minutes",
            sa.Integer,
            nullable=False,
            server_default=sa.text("30"),
        ),
    )
    op.add_column(
        "sources",
        sa.Column(
            "per_item_delay_seconds",
            sa.Integer,
            nullable=False,
            server_default=sa.text("5"),
        ),
    )

    op.execute(
        "UPDATE sources SET interval_minutes = 60, "
        "per_item_delay_seconds = 5 WHERE name = 'steam_market'"
    )
    op.execute(
        "UPDATE sources SET interval_minutes = 15, "
        "per_item_delay_seconds = 0 WHERE name = 'skinport'"
    )
    op.execute(
        "UPDATE sources SET interval_minutes = 15, "
        "per_item_delay_seconds = 3 WHERE name = 'dmarket'"
    )


def downgrade() -> None:
    op.drop_column("sources", "per_item_delay_seconds")
    op.drop_column("sources", "interval_minutes")
