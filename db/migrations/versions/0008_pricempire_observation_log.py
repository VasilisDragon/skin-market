"""pricempire_observation_log: per-(item, source) Pricempire-poll-time

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-17

Phase 2b (ADR 023) — adds a per-pair freshness signal for the
Pricempire bulk-snapshot ingest. Mirrors the existing observation_log
table for the curated collectors (migration 0004), one level up.

The Pricempire collector's dedup gate (ADR 019 §4) suppresses writes
when (price, count) is unchanged. Without a separate log, the freshness
signal we'd be left with is the latest written row's
raw_response->>'last_checked_at' — which goes stale during flat-market
periods even when Pricempire is still actively polling (the swap_gg
pattern from Phase 2a validation §6). That collapses Phase 1's
observed_at bug at one level up.

Schema:
- Regular table (NOT hypertable). Cardinality bounded by deep-tier
  item count × Pricempire sub-provider count ≈ 264 rows max in v1.
- PK (item_id, source_id) — one row per pair, upsert via
  INSERT ... ON CONFLICT DO UPDATE.
- last_observed_at carries Pricempire's claimed `last_checked_at`
  from the cycle's wire row, NOT our local write clock. The drift
  detector reads this to gate stale comparisons.

Backfill:
- One-shot INSERT ... SELECT DISTINCT ON from existing
  pricempire_observations rows. Captures the most-recent
  `last_checked_at` per pair from Phase 2a's accumulated history.
  Closes the "drift detector sees stale on first deploy" window that
  a start-empty approach would leave open.
- ON CONFLICT DO NOTHING is defensive — the table is created in this
  migration and is empty before the INSERT, so there can be no
  conflicts in practice.

Collector wiring (same commit, separate file):
- collectors/pricempire.py gains _upsert_observation_log() helper.
- _persist_row() calls it BEFORE the dedup SELECT. This invariant
  is load-bearing — moving the call inside the dedup branch silently
  reproduces the bug this migration is here to fix.
- tests/test_pricempire_collector.py pins the call ordering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pricempire_observation_log",
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
            comment=(
                "Pricempire's claimed last_checked_at, parsed from the "
                "wire row. Drives the drift detector's freshness gate "
                "(ADR 023). NOT our local write clock."
            ),
        ),
    )

    # Backfill from existing pricempire_observations. The DISTINCT ON
    # picks the most-recent row per (item_id, source_id) by our local
    # `timestamp`; from that row we extract Pricempire's claimed
    # `last_checked_at`. The filter excludes rows where the JSONB key
    # is missing or null (none observed in Phase 2a; defensive).
    #
    # Expected row count at install time on the dev/test stack: 281
    # (per dry-run query 2026-05-17). Matches
    # COUNT(DISTINCT (item_id, source_id)) over pricempire_observations
    # with non-null last_checked_at.
    op.execute(
        """
        INSERT INTO pricempire_observation_log (item_id, source_id, last_observed_at)
        SELECT DISTINCT ON (item_id, source_id)
            item_id,
            source_id,
            (raw_response->>'last_checked_at')::timestamptz AS last_observed_at
        FROM pricempire_observations
        WHERE raw_response ? 'last_checked_at'
          AND (raw_response->>'last_checked_at') IS NOT NULL
        ORDER BY item_id, source_id, timestamp DESC
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("pricempire_observation_log")
