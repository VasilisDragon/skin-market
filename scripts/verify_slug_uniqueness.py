"""Verify slug uniqueness for current DB items and the Pricempire catalog.

The default check mirrors the catalog floor the app can seed today: existing
``items`` rows plus the top 5,000 ranked Pricempire metas by popularity. Use
``--all-metas`` as an exploratory check for the full Pricempire app catalog;
that broader set includes non-skin collectibles the current seeders do not
ingest.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.naming import slugify
from scripts.seed_catalog import CatalogMeta, _load_metas_from_api

DEFAULT_CATALOG_LIMIT = 5000


@dataclass(frozen=True)
class SlugCollision:
    slug: str
    market_hash_names: tuple[str, ...]


def select_ranked_catalog_names(
    metas: Iterable[CatalogMeta],
    *,
    limit: int | None,
) -> list[str]:
    """Return ranked catalog names, deduped by name and sorted by rank."""
    selected: list[str] = []
    seen: set[str] = set()
    for meta in sorted(metas, key=lambda m: (m.rank, m.market_hash_name)):
        if limit is not None and len(selected) >= limit:
            break
        if meta.market_hash_name in seen:
            continue
        seen.add(meta.market_hash_name)
        selected.append(meta.market_hash_name)
    return selected


def detect_slug_collisions(names: Iterable[str]) -> list[SlugCollision]:
    names_by_slug: dict[str, set[str]] = {}
    for name in names:
        names_by_slug.setdefault(slugify(name), set()).add(name)
    return [
        SlugCollision(slug=slug, market_hash_names=tuple(sorted(names)))
        for slug, names in sorted(names_by_slug.items())
        if len(names) > 1
    ]


def load_db_item_names() -> list[str]:
    with Session(get_engine()) as session:
        rows = session.execute(
            text("SELECT market_hash_name FROM items ORDER BY market_hash_name")
        ).all()
    return [row.market_hash_name for row in rows]


def build_name_set(
    *,
    db_names: Iterable[str],
    catalog_names: Iterable[str],
) -> list[str]:
    return sorted({*db_names, *catalog_names})


def print_report(
    *,
    db_count: int,
    catalog_count: int,
    collisions: list[SlugCollision],
) -> None:
    print("Slug uniqueness check:")
    print(f"  DB item names: {db_count}")
    print(f"  Catalog names: {catalog_count}")
    print(f"  Slug collisions: {len(collisions)}")
    for collision in collisions[:20]:
        rendered = ", ".join(collision.market_hash_names[:5])
        if len(collision.market_hash_names) > 5:
            rendered += f", ... (+{len(collision.market_hash_names) - 5} more)"
        print(f"    {collision.slug}: {rendered}")
    if len(collisions) > 20:
        print(f"    ... (+{len(collisions) - 20} more collisions)")


def run(
    *,
    catalog_limit: int | None,
    metas: Iterable[CatalogMeta] | None = None,
    db_names: Iterable[str] | None = None,
) -> int:
    if catalog_limit is not None and catalog_limit < 0:
        print(
            f"ERROR: --catalog-limit must be non-negative, got {catalog_limit}",
            file=sys.stderr,
        )
        return 2

    use_db_names = list(db_names) if db_names is not None else load_db_item_names()
    use_metas = list(metas) if metas is not None else _load_metas_from_api()
    catalog_names = select_ranked_catalog_names(use_metas, limit=catalog_limit)
    names = build_name_set(db_names=use_db_names, catalog_names=catalog_names)
    collisions = detect_slug_collisions(names)
    print_report(
        db_count=len(use_db_names),
        catalog_count=len(catalog_names),
        collisions=collisions,
    )
    return 1 if collisions else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--catalog-limit",
        type=int,
        default=DEFAULT_CATALOG_LIMIT,
        help=(
            "Ranked Pricempire metas to include. Default 5000, matching "
            "the catalog floor. Use with --all-metas to check all metas."
        ),
    )
    parser.add_argument(
        "--all-metas",
        action="store_true",
        help="Check all live Pricempire metas instead of the top-N catalog floor.",
    )
    args = parser.parse_args(argv)

    limit = None if args.all_metas else args.catalog_limit
    try:
        return run(catalog_limit=limit)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
