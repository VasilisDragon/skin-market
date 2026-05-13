"""observation_log: per-(item, source) last-polled timestamp

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-13

Phase 7a — adds ``observation_log(item_id, source_id, last_observed_at)``.
Captures "has source X seen item Y recently?" *pre-dedup*, which the
``prices`` table can't answer because dedup (ADR 009 §3) stops advancing
``prices.timestamp`` for items whose ``(price, volume)`` is stable.

The collector upserts one row per yielded ``PriceObservation`` in
``_run_cycle`` (regardless of whether a ``prices`` row is written).
``analytics.unavailability_streak`` reads from here, not from ``prices``,
so dedup'd observations correctly count as "fresh" rather than missing.

Backfill on upgrade: seed ``observation_log`` from the latest ``prices``
timestamp per ``(item, source)`` so the first analytics cycle after the
migration doesn't see every dedup'd item as suddenly-missing. New
sources or items inserted after this migration get their first row
written by the collector's next observation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observation_log",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id"),
            primary_key=True,
        ),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("sources.id"),
            primary_key=True,
        ),
        sa.Column(
            "last_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    # Seed from the latest known prices row per (item, source).
    # Items the collector hasn't observed yet end up absent; the streak
    # compute treats absent rows as "never observed" with a null
    # last_seen_observed in its meta.
    op.execute(
        """
        INSERT INTO observation_log (item_id, source_id, last_observed_at)
        SELECT DISTINCT ON (item_id, source_id)
            item_id, source_id, timestamp
        FROM prices
        ORDER BY item_id, source_id, timestamp DESC
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("observation_log")
