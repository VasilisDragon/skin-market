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


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


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
    assert sources_count == 2, (
        f"expected 2 sources after seed, got {sources_count}"
    )
