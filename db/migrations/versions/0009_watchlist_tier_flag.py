"""watchlist tier flag (YAML data-layer change; no SQL)

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-17

No-op; YAML schema_version bumped to 2 in this revision, this stub
keeps alembic_version in lockstep with the codebase release.

The functional change lives in data/watchlist.yaml: schema_version
bumps from 1 to 2 and every item gains a `tier: deep | broad` field.
The YAML loader (scripts/seed_watchlist.py and the watchlist readers)
rejects schema_version != 2 from this revision onward.

No Postgres schema changes — the `tier` field is YAML-side only. The
items table doesn't gain a tier column; the watchlist YAML is the
single source of truth for tier membership. ADR 024 documents the
two-tier architecture and the rationale for keeping tier in YAML
rather than denormalizing into the items table.

Per-file YAML schema_version semantics: data/watchlist.yaml and
data/pattern_sensitivity.yaml each carry their own schema_version
field; the versions are independent and bumped on their own file-
level schema changes. They are NOT lockstep. See ADR 021.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No-op. The YAML schema_version bumped from 1 to 2 in this
    # release; see data/watchlist.yaml and ADR 024.
    pass


def downgrade() -> None:
    # No-op. Downgrade requires manually reverting data/watchlist.yaml
    # to schema_version 1 (drop the `tier:` fields from each item) and
    # replacing the loader with the v1 variant.
    pass
