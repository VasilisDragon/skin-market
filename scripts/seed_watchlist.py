"""Seed the ``items`` and ``sources`` tables from ``data/watchlist.yaml``.

Idempotent: re-running with the same YAML is a no-op for rows that already
exist (ON CONFLICT DO NOTHING on the unique constraints). It does NOT delete
rows that exist in the DB but no longer appear in the YAML — handle removals
manually or via a migration.

Usage:
    uv run python -m scripts.seed_watchlist
    uv run python -m scripts.seed_watchlist --watchlist /path/to/watchlist.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.naming import normalize_name, slugify

# Default location. Resolved relative to the repo root so the script works
# from any cwd as long as the repo layout is intact.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WATCHLIST_PATH = _REPO_ROOT / "data" / "watchlist.yaml"

# Bump this if seed_watchlist.py's expectations of the YAML schema change
# incompatibly. The YAML file declares its own ``schema_version`` we check.
_SUPPORTED_SCHEMA_VERSION = 1


_INSERT_SOURCE_SQL = text(
    """
    INSERT INTO sources (name, base_url, rate_limit_per_minute, enabled, denomination)
    VALUES (:name, :base_url, :rlim, :enabled, :denom)
    ON CONFLICT (name) DO UPDATE SET
        base_url = EXCLUDED.base_url,
        rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
        enabled = EXCLUDED.enabled,
        -- COALESCE so a partial seed (e.g. test fixture YAML without
        -- denomination) leaves the existing value intact rather than
        -- silently clobbering it to NULL.
        denomination = COALESCE(EXCLUDED.denomination, sources.denomination)
    """
)

_INSERT_ITEM_SQL = text(
    """
    INSERT INTO items (
        market_hash_name, display_name, slug, item_type,
        weapon_name, skin_name, wear, is_stattrak, is_souvenir
    )
    VALUES (
        :mhn, :disp, :slug, :it,
        :wpn, :skn, :wear, :stt, :sv
    )
    ON CONFLICT (market_hash_name) DO NOTHING
    """
)


def load_watchlist(path: Path) -> dict[str, Any]:
    """Parse the watchlist YAML and verify its schema_version."""
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    version = data.get("schema_version")
    if version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version is {version!r}, expected "
            f"{_SUPPORTED_SCHEMA_VERSION}. Update seed_watchlist.py or the YAML."
        )
    if "sources" not in data or "items" not in data:
        raise ValueError(f"{path}: missing required top-level keys 'sources' and 'items'")
    return data


def seed(watchlist_path: Path = DEFAULT_WATCHLIST_PATH) -> tuple[int, int]:
    """Run the seed. Returns (items_in_db, sources_in_db) after the upsert."""
    data = load_watchlist(watchlist_path)

    engine = get_engine()
    with Session(engine) as session:
        for src in data["sources"]:
            session.execute(
                _INSERT_SOURCE_SQL,
                {
                    "name": src["name"],
                    "base_url": src.get("base_url"),
                    "rlim": src.get("rate_limit_per_minute"),
                    "enabled": src.get("enabled", True),
                    "denom": src.get("denomination"),
                },
            )
        for it in data["items"]:
            mhn = normalize_name(it["market_hash_name"])
            session.execute(
                _INSERT_ITEM_SQL,
                {
                    "mhn": mhn,
                    # v1: display_name == market_hash_name. The schema keeps
                    # them separate so v2+ can prettify (e.g. drop "StatTrak™").
                    "disp": it.get("display_name", mhn),
                    "slug": slugify(mhn),
                    "it": it.get("item_type"),
                    "wpn": it.get("weapon_name"),
                    "skn": it.get("skin_name"),
                    "wear": it.get("wear"),
                    "stt": bool(it.get("is_stattrak", False)),
                    "sv": bool(it.get("is_souvenir", False)),
                },
            )
        session.commit()

    with engine.connect() as conn:
        items_count = conn.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
        sources_count = conn.execute(text("SELECT COUNT(*) FROM sources")).scalar_one()
    return int(items_count), int(sources_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help=f"Path to watchlist YAML (default: {DEFAULT_WATCHLIST_PATH})",
    )
    args = parser.parse_args(argv)

    items_count, sources_count = seed(args.watchlist)
    print(f"Seed complete: {items_count} items, {sources_count} sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
