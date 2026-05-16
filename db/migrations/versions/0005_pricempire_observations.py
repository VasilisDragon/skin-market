"""pricempire_observations hypertable + sub-provider source rows

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-16

Phase 2a Step 1 — adds Pricempire as a breadth-coverage data source
layered on top of the existing curated Steam/Skinport/DMarket
collectors. ADR 018 has the design rationale (why a separate
hypertable instead of reusing ``prices``); ADR 019 has the collector
design (bulk-snapshot, stream-parse, distinct from the BaseCollector
abstraction).

What this migration creates:

1. ``pricempire_observations`` — the time-series store, one row per
   ``(item, sub_provider)`` per snapshot Pricempire cycle. Composite PK
   ``(item_id, source_id, timestamp)`` mirrors ``prices``. Converted to
   a TimescaleDB hypertable on ``timestamp`` with the same 7-day
   chunking. Compression policy is deliberately NOT added in this
   migration — Phase 2a is "get data flowing"; storage policies are
   deferred until table size warrants tuning.

2. Six **sub-provider** rows in ``sources`` — ``pricempire_buff163``,
   ``pricempire_buff163_buy``, ``pricempire_skinport``,
   ``pricempire_dmarket``, ``pricempire_csmoney``,
   ``pricempire_swap_gg``. All start ``enabled = FALSE``. Step 3 of
   Phase 2a (the scheduler-wire commit) flips these to TRUE once the
   collector is actually running and writing rows.

3. One ``pricempire`` **pseudo-source** row in ``sources`` —
   ``enabled = TRUE``, ``interval_minutes = 15``. The scheduler keys
   off this row to fire the bulk-snapshot collector once per
   interval. The six sub-provider rows are NOT independently
   scheduled — one Pricempire HTTP call writes rows for all of them.
   ADR 018 §3 explains why we keep both the pseudo-source and the
   sub-providers rather than collapsing them.

About the three timestamps in pricempire_observations
-----------------------------------------------------

Phase 1 (ADR 017) taught the project to be precise about what each
freshness field actually represents. Pricempire's payload carries TWO
provider-asserted timestamps per price row, neither of which is "when
we wrote this." So pricempire_observations carries THREE:

- ``timestamp`` (the local clock when we wrote the row): drives our
  own dedup gate, observation_log analog, and TimescaleDB
  chunking. This is the project's canonical "when did we record
  this" field.
- ``last_checked_at`` (Pricempire's ``last_checked_at``): when
  Pricempire claims it polled the provider. Drives Phase 2b drift
  detection — if last_checked_at lags badly behind timestamp,
  Pricempire isn't actually refreshing.
- ``updated_at`` (Pricempire's ``updated_at``): when Pricempire
  thinks the underlying price actually moved. Note: empirically,
  Skinport rows carry a placeholder ``2025-01-01T00:00:00.000Z``
  here while ``last_checked_at`` is real-time. Phase 2b drift logic
  must handle this.

Treating these as three distinct concepts is the load-bearing lesson
from Phase 1. Do not collapse them.

About the dedup gate
--------------------

Phase 2a applies dedup-on-write the same way the prices collectors
do (ADR 009 §3): skip the insert when ``(price, count)`` matches the
latest row for that ``(item_id, source_id)``. There is no
observation_log analog for Pricempire in Phase 2a — drift detection
in Phase 2b decides whether one is needed. Until then, the dedup
gate's "did we just see the same price+count" check operates against
``pricempire_observations`` itself.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The six Pricempire sub-providers we ingest in Phase 2a. Sourced from
# the empirical findings in docs/pre-phase2-pricempire-diagnostic.md
# §Call 3 (buff163, buff163_buy, skinport, dmarket) plus the two
# additional middle-market providers Pricempire serves (csmoney,
# swap.gg). All quoted in USD per the diagnostic's per-row inspection.
_SUB_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("pricempire_buff163", "usd"),
    ("pricempire_buff163_buy", "usd"),
    ("pricempire_skinport", "usd"),
    ("pricempire_dmarket", "usd"),
    ("pricempire_csmoney", "usd"),
    ("pricempire_swap_gg", "usd"),
)


def upgrade() -> None:
    # ── 1. pricempire_observations hypertable ──────────────────────
    op.create_table(
        "pricempire_observations",
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
            "timestamp",
            sa.DateTime(timezone=True),
            primary_key=True,
            nullable=False,
            server_default=sa.func.now(),
            comment=(
                "Local clock at row-write time. Drives dedup, "
                "TimescaleDB chunking, and the project's canonical "
                "freshness fields. NOT Pricempire's last_checked_at."
            ),
        ),
        sa.Column(
            "price",
            sa.Numeric(12, 2),
            nullable=False,
            comment=(
                "USD price. Pricempire returns cents in the wire "
                "payload (e.g. 17316 for $173.16); the collector "
                "divides by 100 before insert."
            ),
        ),
        sa.Column(
            "count",
            sa.Integer,
            comment=(
                "Per-provider listing count. Pricempire calls this "
                "`count`, distinct from the existing prices.volume "
                "concept (Steam 24h sales)."
            ),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            comment=(
                "Pricempire's reported 'price last changed' "
                "timestamp. Informational. Empirically Skinport "
                "carries a 2025-01-01 placeholder here — Phase 2b "
                "drift logic must tolerate this."
            ),
        ),
        sa.Column(
            "last_checked_at",
            sa.DateTime(timezone=True),
            comment=(
                "Pricempire's reported 'we polled the provider' "
                "timestamp. Drives Phase 2b drift detection — if "
                "this lags timestamp, Pricempire isn't refreshing."
            ),
        ),
        sa.Column(
            "currency",
            sa.String(8),
            nullable=False,
            server_default="USD",
        ),
        sa.Column("raw_response", JSONB),
    )

    # Convert to TimescaleDB hypertable. Match the prices table's
    # 7-day chunking — at ~48 items × 6 providers × 96 cycles/day
    # raw volume the dedup gate cuts this substantially, projected
    # ~5-10x smaller than prices in steady state.
    op.execute(
        "SELECT create_hypertable('pricempire_observations', "
        "'timestamp', chunk_time_interval => INTERVAL '7 days')"
    )

    # Latest-row-per-(item, source) is the dominant access pattern
    # for drift detection (Phase 2b) and the eventual /lookup
    # endpoint. DESC index answers it with one seek.
    op.execute(
        "CREATE INDEX ix_pricempire_observations_timestamp_desc "
        "ON pricempire_observations (timestamp DESC)"
    )

    # ── 2. Six sub-provider source rows (disabled until Step 3) ────
    #
    # interval_minutes and per_item_delay_seconds are meaningless for
    # these rows because the six sub-providers are NOT independently
    # scheduled — one bulk Pricempire call services all of them.
    # They share the schedule of the `pricempire` pseudo-source below.
    # The sources schema requires NOT NULL on both columns (defaults
    # 30 and 5), so we set them to 0 and rely on the scheduler
    # special-casing the `pricempire_*` rows.
    for name, denomination in _SUB_PROVIDERS:
        op.execute(
            sa.text(
                "INSERT INTO sources "
                "(name, base_url, rate_limit_per_minute, enabled, "
                " denomination, interval_minutes, "
                " per_item_delay_seconds) "
                "VALUES (:name, NULL, NULL, FALSE, :denom, 0, 0) "
                "ON CONFLICT (name) DO NOTHING"
            ).bindparams(name=name, denom=denomination)
        )

    # ── 3. The `pricempire` pseudo-source row ──────────────────────
    #
    # The scheduler reads this row to fire the bulk-snapshot
    # collector. denomination is left NULL — this row never carries
    # prices itself, only the schedule.
    op.execute(
        sa.text(
            "INSERT INTO sources "
            "(name, base_url, rate_limit_per_minute, enabled, "
            " denomination, interval_minutes, "
            " per_item_delay_seconds) "
            "VALUES ('pricempire', "
            "        'https://api.pricempire.com', "
            "        NULL, TRUE, NULL, 15, 0) "
            "ON CONFLICT (name) DO NOTHING"
        )
    )


def downgrade() -> None:
    # DROP TABLE first — the sub-provider sources rows are referenced
    # by pricempire_observations.source_id, and Postgres won't let us
    # DELETE those rows while the FK exists. CASCADE on DROP TABLE
    # also tears down any chunks the hypertable accumulated. This
    # order is load-bearing once the table has live data; the
    # original DELETE-first order worked only because the table was
    # empty at first-ever downgrade. Phase 2a-follow-up fix.
    op.execute("DROP TABLE IF EXISTS pricempire_observations CASCADE")
    op.execute(
        "DELETE FROM sources WHERE name IN "
        "('pricempire', 'pricempire_buff163', 'pricempire_buff163_buy', "
        " 'pricempire_skinport', 'pricempire_dmarket', "
        " 'pricempire_csmoney', 'pricempire_swap_gg')"
    )
