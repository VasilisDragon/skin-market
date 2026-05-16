"""pricempire hypertable compression policies

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-17

Phase 2b — lifts the ADR 018 §"Consequences" deferral on compression
for the Pricempire hypertables. With broad-tier expansion projected
to cross the deferral's trigger condition ("first hot chunk crosses
~100k rows or storage becomes a concern") within ~1 month of deploy,
compression is installed now while the change is non-destructive.

Mirrors migration 0001's compression setup on `prices`:

- segment_by chooses the natural per-series grouping. For
  pricempire_observations, that's (item_id, source_id) — matches the
  composite PK shape and the per-pair access pattern. For
  pricempire_item_metadata, it's just `item_id` because there is no
  source dimension on the metadata table (PK is (item_id, timestamp)).
- compress_after = 30 days. Chunks younger than 30d remain in row
  storage for write throughput. Older chunks get columnar compression;
  ~10-20x typical compression ratio for time-series segmented by
  series.

ADR 025 documents the deferral lift, the segment-by rationale per
table, and the explicit non-scope of retention.

Safety: chunks must be older than the compress_after interval before
the policy fires. At install time, no chunk on either Pricempire
table is older than ~24h. The policy will not run on existing data;
the migration is non-destructive at install time.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pricempire_observations — segment by the (item, source) pair
    # so each compressed segment groups one item-source's time series.
    # Same shape as migration 0001's compression on `prices`.
    op.execute(
        """
        ALTER TABLE pricempire_observations SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'item_id, source_id'
        )
        """
    )
    op.execute(
        "SELECT add_compression_policy("
        "  'pricempire_observations', INTERVAL '30 days'"
        ")"
    )

    # pricempire_item_metadata — segment by item_id only, since there
    # is no source dimension (PK is (item_id, timestamp)).
    op.execute(
        """
        ALTER TABLE pricempire_item_metadata SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'item_id'
        )
        """
    )
    op.execute(
        "SELECT add_compression_policy("
        "  'pricempire_item_metadata', INTERVAL '30 days'"
        ")"
    )


def downgrade() -> None:
    # Remove policies first, then disable compression. If any chunks
    # have already been compressed at downgrade time, the
    # `compress = false` ALTER will fail until those chunks are
    # decompressed via decompress_chunk(). At Phase 2b install time,
    # no chunks are old enough to have been compressed, so downgrade
    # is safe.
    op.execute(
        "SELECT remove_compression_policy('pricempire_item_metadata')"
    )
    op.execute(
        "ALTER TABLE pricempire_item_metadata SET ("
        "  timescaledb.compress = false"
        ")"
    )
    op.execute(
        "SELECT remove_compression_policy('pricempire_observations')"
    )
    op.execute(
        "ALTER TABLE pricempire_observations SET ("
        "  timescaledb.compress = false"
        ")"
    )
