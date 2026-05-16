"""enable pricempire sub-provider sources

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-16

Phase 2a Step 3 — flips the six ``pricempire_*`` sub-provider source
rows to ``enabled = TRUE``. Migration 0005 introduced them as
``enabled = FALSE`` so the schema-only commit didn't go-live ahead of
the scheduler-wire commit; this migration is the "infrastructure goes
live" half of that pair.

Semantics of the flag on these rows:

The six sub-providers are NOT independently scheduled. They share the
``pricempire`` pseudo-source's APScheduler job (one Pricempire HTTP
call services all six). The scheduler explicitly skips
``pricempire_*`` rows when iterating enabled sources. So
``enabled = TRUE`` here does NOT mean "schedule a per-source job"; it
means "yes, the system is actively ingesting data for this
sub-provider, treat the source as live."

The flag is read by analytics + API queries that filter
``WHERE s.enabled = TRUE`` on the ``prices`` table. None of those
queries reach ``pricempire_observations`` today — Pricempire data is
on its own hypertable (migration 0005). So flipping the flag has zero
behavioral effect on existing endpoints; it's purely an operational
declaration that Phase 2b consumers can treat these sub-providers as
live data sources.

ADR 018 §3 documents the pseudo-source / sub-provider split.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SUB_PROVIDER_NAMES: tuple[str, ...] = (
    "pricempire_buff163",
    "pricempire_buff163_buy",
    "pricempire_skinport",
    "pricempire_dmarket",
    "pricempire_csmoney",
    "pricempire_swap_gg",
)


def upgrade() -> None:
    # SAFE: only flips the named sub-provider rows. Production sources
    # (steam_market, skinport, dmarket) and the `pricempire`
    # pseudo-source are untouched.
    op.execute(
        "UPDATE sources SET enabled = TRUE WHERE name IN ("
        + ", ".join(f"'{n}'" for n in _SUB_PROVIDER_NAMES)
        + ")"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sources SET enabled = FALSE WHERE name IN ("
        + ", ".join(f"'{n}'" for n in _SUB_PROVIDER_NAMES)
        + ")"
    )
