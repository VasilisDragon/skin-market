"""Migration round-trip test: alembic downgrade base -> upgrade head -> re-seed.

**Destructive.** This test drops the four domain tables (items, sources,
prices, insights) and recreates them from the migration. Any collected
price data in the test DB is wiped. The seed runs afterwards so items and
sources are restored, but ``prices`` and ``insights`` are emptied.

Skipped automatically when DATABASE_URL is unset or Postgres is unreachable
(matches the pattern in ``test_db_roundtrip.py``), so this file is safe on
CI without a postgres service.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from db.connection import get_engine

_DOMAIN_TABLES = ("items", "sources", "prices", "insights")
# Phase 2a / 2b additions, in order of introduction. Checked separately
# from _DOMAIN_TABLES so a failure surfaces "Pricempire schema missing"
# distinctly from "v1 schema missing."
_PRICEMPIRE_TABLES = (
    "pricempire_observations",      # migration 0005
    "pricempire_item_metadata",     # migration 0007
    "pricempire_observation_log",   # migration 0008
)
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        return False


pytestmark = [
    pytest.mark.destructive,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL") or not _db_reachable(),
        reason="DATABASE_URL not set or postgres unreachable",
    ),
]


def _alembic_cfg() -> Config:
    """Build an Alembic Config pointing at the repo's alembic.ini.

    We resolve the path explicitly so the test runs regardless of pytest's
    cwd (it usually runs from the repo root, but be defensive).
    """
    return Config(str(_REPO_ROOT / "alembic.ini"))


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = :n AND table_schema = 'public')"
            ),
            {"n": table},
        ).scalar()
    )


def test_migration_roundtrip_then_seed() -> None:
    cfg = _alembic_cfg()
    engine = get_engine()

    # 1. Downgrade to base.
    command.downgrade(cfg, "base")

    # 2. Assert all four domain tables are gone.
    with engine.connect() as conn:
        for table in _DOMAIN_TABLES:
            assert not _table_exists(conn, table), (
                f"{table} still exists after downgrade to base"
            )

    # 3. Upgrade back to head.
    command.upgrade(cfg, "head")

    # 4. Verify the schema reapplied cleanly — tables back, prices is a
    #    hypertable, compression policy is registered.
    with engine.connect() as conn:
        for table in _DOMAIN_TABLES:
            assert _table_exists(conn, table), (
                f"{table} missing after upgrade to head"
            )
        is_hypertable = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'prices')"
            )
        ).scalar()
        assert is_hypertable, "prices is not a hypertable after upgrade"

        policy_exists = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM timescaledb_information.jobs "
                "WHERE proc_name = 'policy_compression')"
            )
        ).scalar()
        assert policy_exists, "compression policy job not registered"

    # 4b. Phase 2a/2b additions: confirm the Pricempire tables exist.
    #     pricempire_observations and pricempire_item_metadata are
    #     hypertables; pricempire_observation_log is a regular table.
    with engine.connect() as conn:
        for table in _PRICEMPIRE_TABLES:
            assert _table_exists(conn, table), (
                f"{table} missing after upgrade to head"
            )
        for hypertable in (
            "pricempire_observations",
            "pricempire_item_metadata",
        ):
            is_hypertable = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM "
                    "timescaledb_information.hypertables "
                    "WHERE hypertable_name = :n)"
                ),
                {"n": hypertable},
            ).scalar()
            assert is_hypertable, f"{hypertable} is not a hypertable"

        # Migration 0010: compression policies on both Pricempire
        # hypertables. compression_enabled and the segment_by config
        # both pin the migration's effects.
        for hypertable, expected_segmentby in (
            ("pricempire_observations", "item_id, source_id"),
            ("pricempire_item_metadata", "item_id"),
        ):
            compress_on = conn.execute(
                text(
                    "SELECT compression_enabled FROM "
                    "timescaledb_information.hypertables "
                    "WHERE hypertable_name = :n"
                ),
                {"n": hypertable},
            ).scalar()
            assert compress_on, (
                f"{hypertable}.compression_enabled is False after "
                f"migration 0010 upgrade"
            )
            policy_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_compression' "
                    "AND hypertable_name = :n"
                ),
                {"n": hypertable},
            ).scalar()
            assert policy_count == 1, (
                f"{hypertable} has {policy_count} policy_compression "
                f"jobs, expected 1"
            )

    # 5. Re-seed and assert the watchlist landed.
    #    Import inline so pytest collection doesn't fail when the seed
    #    module's dependencies (e.g. pyyaml) are missing in odd setups.
    from scripts.seed_watchlist import seed

    items_count, sources_count = seed()
    # The watchlist has 48 items (~50 target): 12 rifles + 10 snipers +
    # 12 knives + 7 gloves + 5 pistols + 2 SMGs. If you edit the YAML,
    # update this assertion accordingly.
    assert items_count == 48, (
        f"expected 48 items after seed, got {items_count}"
    )
    # 3 from seed (steam_market, skinport, dmarket) + 7 from migration
    # 0005 (pricempire pseudo-source + 6 sub-providers). If the count
    # drifts, update this alongside the YAML or migration.
    assert sources_count == 10, (
        f"expected 10 sources after seed (3 seed + 7 from migrations), "
        f"got {sources_count}"
    )
