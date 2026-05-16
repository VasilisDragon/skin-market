"""pricempire_item_metadata: per-item slow-changing fields

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-16

Phase 2a follow-up — extracts the item-level metadata fields
Pricempire's ``/v4/paid/items/prices`` response carries alongside the
nested per-provider ``prices`` array. These fields describe the *item*
(rank, liquidity, marketcap, Steam-side trade volumes) rather than
any individual provider's price; storing them in
``pricempire_observations.raw_response`` JSONB hides them behind
hand-rolled JSON traversal at query time. Lifting them into typed
columns makes them queryable and indexable.

About the dedup-friendly hypertable shape
-----------------------------------------

The metadata fields change slowly (rank shifts a few positions per
day at most; marketcap drifts gradually; the steam_last_* counters
update once per day on Pricempire's side). Most cycles will write
zero rows because the dedup gate will see no change. At our 15-min
Pricempire cadence the steady-state row volume should be on the
order of 1-5 rows per item per day, not 96.

The shape mirrors ``pricempire_observations``:

- Composite PK ``(item_id, timestamp)`` — one row per *change*, not
  one per cycle. ``source_id`` is NOT part of the key because these
  fields are per-item, not per-provider.
- TimescaleDB hypertable on ``timestamp`` with the same 7-day
  chunking as the price tables. No compression policy in this
  migration; revisit if storage warrants.
- An index on ``(item_id, timestamp DESC)`` answers the dominant
  "latest metadata for item X" query with one seek.

About the columns
-----------------

Eleven of these fields ship on every ``/v4/paid/items/prices`` item
that has any priced provider. ``steam_last_24h`` is the exception —
it appears on the ``/v4/paid/items/metas`` endpoint only, not on
``/prices``. We include the column for forward compatibility with a
hypothetical Phase 2b metas-cron, even though the Phase 2a collector
will always write NULL here. ADR 020 documents the decision.

All metadata columns are NULL-able. Pricempire returns nulls for
low-liquidity items (e.g. ``trades_7d`` is often null for rare
souvenir items). The dedup gate compares the full tuple, NULLs
included, so a missing field is treated as "no change" rather than
"changed to null."

Pricempire's wire types are inconsistent across the two endpoints —
on ``/prices`` the integer-valued fields come as numeric strings
(e.g. ``"rank": "554"``) while ``/metas`` returns native numbers
(``"rank": 23219``). The collector parses defensively (see
``collectors/pricempire.py`` and ADR 020 §3); the columns are typed
to match the natural value range (BIGINT for ``marketcap`` since the
top-end values are ~10⁹).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pricempire_item_metadata",
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id"),
            primary_key=True,
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            primary_key=True,
            nullable=False,
            server_default=sa.func.now(),
            comment=(
                "Local clock at row-write time. Drives the dedup "
                "gate and TimescaleDB chunking. Same semantic as "
                "pricempire_observations.timestamp."
            ),
        ),
        # Item-level signals from Pricempire's response. All nullable
        # — Pricempire returns nulls for low-liquidity items.
        sa.Column(
            "rank",
            sa.Integer,
            comment=(
                "Pricempire popularity rank (lower = more popular). "
                "Wire shape: numeric string on /prices, native int "
                "on /metas. Defensive parse in the collector."
            ),
        ),
        sa.Column(
            "liquidity",
            sa.Numeric(6, 2),
            comment=(
                "Pricempire's liquidity score, 0-100. Wire shape: "
                "native number on both endpoints (float-shaped)."
            ),
        ),
        sa.Column(
            "marketcap",
            sa.BigInteger,
            comment=(
                "Pricempire's marketcap (~10⁹ at the high end — "
                "BIGINT not INT). Wire: numeric string on /prices."
            ),
        ),
        sa.Column(
            "count",
            sa.Integer,
            comment=(
                "Item-level total listing count (NOT the same as "
                "pricempire_observations.count, which is "
                "per-provider). Wire: numeric string on /prices."
            ),
        ),
        sa.Column("trades_7d", sa.Integer),
        sa.Column("trades_30d", sa.Integer),
        sa.Column("trades_90d", sa.Integer),
        sa.Column(
            "steam_last_24h",
            sa.Integer,
            comment=(
                "Steam 24h trade count. Only present on Pricempire's "
                "/metas endpoint; the Phase 2a collector writes from "
                "/prices and will always leave this NULL. Reserved "
                "for a future metas-cron."
            ),
        ),
        sa.Column("steam_last_7d", sa.Integer),
        sa.Column("steam_last_30d", sa.Integer),
        sa.Column("steam_last_90d", sa.Integer),
    )

    op.execute(
        "SELECT create_hypertable('pricempire_item_metadata', "
        "'timestamp', chunk_time_interval => INTERVAL '7 days')"
    )

    op.execute(
        "CREATE INDEX ix_pricempire_item_metadata_item_ts_desc "
        "ON pricempire_item_metadata (item_id, timestamp DESC)"
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS pricempire_item_metadata CASCADE"
    )
