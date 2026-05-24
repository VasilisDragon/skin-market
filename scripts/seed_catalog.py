"""Seed long-tail catalog items from Pricempire ``/v4/paid/items/metas``.

Bulk seed: insert the top ranked Pricempire catalog names into ``items`` so
they become substrate-tier items (present in DB, absent from
``data/watchlist.yaml``). Curated and featured tiers remain YAML-owned; this
script does not edit the watchlist.

Usage:
    uv run python -m scripts.seed_catalog --dry-run
    uv run python -m scripts.seed_catalog
    uv run python -m scripts.seed_catalog --limit 5000
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx
import ijson
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.naming import normalize_name, slugify
from scripts.seed_watchlist import (
    _INSERT_ITEM_SQL,
    DEFAULT_WATCHLIST_PATH,
    load_watchlist,
)

PRICEMPIRE_BASE_URL = "https://api.pricempire.com"
PRICEMPIRE_METAS_PATH = "/v4/paid/items/metas"
DEFAULT_LIMIT = 5000
_APP_ID = "730"
_HTTP_TIMEOUT_SECONDS = 90.0
_SAMPLE_SIZE_IN_REPORT = 8


@dataclass(frozen=True)
class CatalogMeta:
    market_hash_name: str
    rank: int


@dataclass(frozen=True)
class SeedCandidate:
    market_hash_name: str
    rank: int
    slug: str
    is_stattrak: bool
    is_souvenir: bool


@dataclass(frozen=True)
class SlugCollision:
    slug: str
    market_hash_names: tuple[str, ...]


@dataclass(frozen=True)
class ExistingItem:
    market_hash_name: str
    slug: str


@dataclass(frozen=True)
class SeedPlan:
    candidates: list[SeedCandidate]
    collisions: list[SlugCollision]
    metas_seen: int
    ranked_seen: int
    duplicate_names_skipped: int
    exclusions_skipped: int
    existing_candidates: int
    new_candidates: int
    existing_total: int


def _api_key() -> str:
    api_key = os.environ.get("PRICEMPIRE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "PRICEMPIRE_API_KEY is unset. Set it in .env and retry."
        )
    return api_key


def _make_client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=PRICEMPIRE_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=httpx.Timeout(
            connect=10.0,
            read=_HTTP_TIMEOUT_SECONDS,
            write=10.0,
            pool=10.0,
        ),
    )


def _stream_metas_from_content(content: bytes) -> Iterator[dict[str, Any]]:
    yield from ijson.items(io.BytesIO(content), "item", use_float=True)


def fetch_metas(client: httpx.Client) -> list[CatalogMeta]:
    response = client.get(PRICEMPIRE_METAS_PATH, params={"app_id": _APP_ID})
    response.raise_for_status()
    return parse_metas(_stream_metas_from_content(response.content))


def parse_metas(items: Iterable[Mapping[str, Any]]) -> list[CatalogMeta]:
    metas: list[CatalogMeta] = []
    for item in items:
        raw_name = item.get("market_hash_name")
        rank = _coerce_rank(item.get("rank"))
        if not isinstance(raw_name, str) or not raw_name.strip() or rank is None:
            continue
        metas.append(
            CatalogMeta(
                market_hash_name=normalize_name(raw_name.strip()),
                rank=rank,
            )
        )
    return metas


def _coerce_rank(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        try:
            return int(raw)
        except (OverflowError, ValueError):
            return None
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            try:
                return int(float(value))
            except (OverflowError, ValueError):
                return None
    return None


def detect_flags(market_hash_name: str) -> tuple[bool, bool]:
    return (
        market_hash_name.startswith("StatTrak™ "),
        market_hash_name.startswith("Souvenir "),
    )


def load_exclusions(watchlist_path: Path) -> set[str]:
    data = load_watchlist(watchlist_path)
    raw = data.get("featured_tier_exclusions") or []
    if not isinstance(raw, list):
        raise ValueError(
            f"{watchlist_path}: featured_tier_exclusions must be a list"
        )
    return {
        normalize_name(item)
        for item in raw
        if isinstance(item, str) and item.strip()
    }


def load_existing_items(session: Session) -> list[ExistingItem]:
    rows = session.execute(
        text("SELECT market_hash_name, slug FROM items ORDER BY market_hash_name")
    ).all()
    return [
        ExistingItem(market_hash_name=r.market_hash_name, slug=r.slug)
        for r in rows
    ]


def build_candidates(
    metas: Iterable[CatalogMeta],
    *,
    exclusions: set[str],
    limit: int,
) -> tuple[list[SeedCandidate], int]:
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")

    candidates: list[SeedCandidate] = []
    seen_names: set[str] = set()
    duplicate_names_skipped = 0

    for meta in sorted(metas, key=lambda m: (m.rank, m.market_hash_name)):
        if len(candidates) >= limit:
            break
        if meta.market_hash_name in seen_names:
            duplicate_names_skipped += 1
            continue
        seen_names.add(meta.market_hash_name)
        if meta.market_hash_name in exclusions:
            continue

        is_stattrak, is_souvenir = detect_flags(meta.market_hash_name)
        candidates.append(
            SeedCandidate(
                market_hash_name=meta.market_hash_name,
                rank=meta.rank,
                slug=slugify(meta.market_hash_name),
                is_stattrak=is_stattrak,
                is_souvenir=is_souvenir,
            )
        )

    return candidates, duplicate_names_skipped


def detect_slug_collisions(
    *,
    candidates: Iterable[SeedCandidate],
    existing_items: Iterable[ExistingItem],
) -> list[SlugCollision]:
    names_by_slug: dict[str, set[str]] = {}

    for item in existing_items:
        names_by_slug.setdefault(item.slug, set()).add(item.market_hash_name)
    for candidate in candidates:
        names_by_slug.setdefault(candidate.slug, set()).add(
            candidate.market_hash_name
        )

    collisions = [
        SlugCollision(slug=slug, market_hash_names=tuple(sorted(names)))
        for slug, names in names_by_slug.items()
        if len(names) > 1
    ]
    return sorted(collisions, key=lambda c: c.slug)


def build_seed_plan(
    *,
    metas: list[CatalogMeta],
    exclusions: set[str],
    existing_items: list[ExistingItem],
    limit: int,
) -> SeedPlan:
    candidates, duplicate_names_skipped = build_candidates(
        metas,
        exclusions=exclusions,
        limit=limit,
    )
    collisions = detect_slug_collisions(
        candidates=candidates,
        existing_items=existing_items,
    )
    existing_names = {item.market_hash_name for item in existing_items}
    candidate_names = {c.market_hash_name for c in candidates}
    exclusions_skipped = sum(1 for meta in metas if meta.market_hash_name in exclusions)

    return SeedPlan(
        candidates=candidates,
        collisions=collisions,
        metas_seen=len(metas),
        ranked_seen=len(metas),
        duplicate_names_skipped=duplicate_names_skipped,
        exclusions_skipped=exclusions_skipped,
        existing_candidates=len(candidate_names & existing_names),
        new_candidates=len(candidate_names - existing_names),
        existing_total=len(existing_items),
    )


def insert_candidates(session: Session, candidates: Iterable[SeedCandidate]) -> int:
    inserted = 0
    for candidate in candidates:
        result = session.execute(
            _INSERT_ITEM_SQL,
            {
                "mhn": candidate.market_hash_name,
                "disp": candidate.market_hash_name,
                "slug": candidate.slug,
                "it": None,
                "wpn": None,
                "skn": None,
                "wear": None,
                "stt": candidate.is_stattrak,
                "sv": candidate.is_souvenir,
            },
        )
        inserted += int(result.rowcount or 0)
    return inserted


def insert_candidates_in_transaction(
    session: Session,
    candidates: Iterable[SeedCandidate],
) -> int:
    try:
        inserted = insert_candidates(session, candidates)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return inserted


def _format_sample(items: Iterable[str], cap: int = _SAMPLE_SIZE_IN_REPORT) -> str:
    names = list(items)
    if not names:
        return "(none)"
    rendered = ", ".join(names[:cap])
    if len(names) > cap:
        rendered += f", ... (+{len(names) - cap} more)"
    return rendered


def print_plan(
    plan: SeedPlan,
    *,
    limit: int,
    file: TextIO | None = None,
) -> None:
    out = file if file is not None else sys.stdout
    print("Catalog seed plan:", file=out)
    print(f"  Source: Pricempire {PRICEMPIRE_METAS_PATH}", file=out)
    print(f"  Limit: {limit}", file=out)
    print(
        f"  Ranked metas parsed: {plan.ranked_seen}; "
        f"candidates selected: {len(plan.candidates)}",
        file=out,
    )
    print(
        f"  Existing DB items: {plan.existing_total}; "
        f"already present in selected set: {plan.existing_candidates}; "
        f"new inserts planned: {plan.new_candidates}",
        file=out,
    )
    print(
        f"  Exclusion matches in ranked metas: {plan.exclusions_skipped}; "
        f"duplicate names skipped before cutoff: {plan.duplicate_names_skipped}",
        file=out,
    )
    print(f"  Slug collisions: {len(plan.collisions)}", file=out)
    if plan.collisions:
        for collision in plan.collisions[:_SAMPLE_SIZE_IN_REPORT]:
            print(
                f"    {collision.slug}: "
                f"{_format_sample(collision.market_hash_names)}",
                file=out,
            )
        if len(plan.collisions) > _SAMPLE_SIZE_IN_REPORT:
            print(
                f"    ... (+{len(plan.collisions) - _SAMPLE_SIZE_IN_REPORT} more)",
                file=out,
            )

    sample = [
        f"{c.market_hash_name} (rank {c.rank})"
        for c in plan.candidates[:_SAMPLE_SIZE_IN_REPORT]
    ]
    print(f"  Top selected: {_format_sample(sample)}", file=out)


def _load_metas_from_api() -> list[CatalogMeta]:
    with _make_client(_api_key()) as client:
        return fetch_metas(client)


def run(
    *,
    watchlist_path: Path,
    limit: int,
    dry_run: bool,
    engine: Engine | None = None,
    metas: list[CatalogMeta] | None = None,
    file: TextIO | None = None,
) -> int:
    out = file if file is not None else sys.stdout
    if limit < 0:
        print(f"ERROR: --limit must be non-negative, got {limit}", file=sys.stderr)
        return 2
    if not watchlist_path.exists():
        print(f"ERROR: watchlist not found at {watchlist_path}", file=sys.stderr)
        return 2

    exclusions = load_exclusions(watchlist_path)
    catalog_metas = metas if metas is not None else _load_metas_from_api()

    use_engine = engine or get_engine()
    with Session(use_engine) as session:
        existing_items = load_existing_items(session)

    plan = build_seed_plan(
        metas=catalog_metas,
        exclusions=exclusions,
        existing_items=existing_items,
        limit=limit,
    )
    print_plan(plan, limit=limit, file=out)

    if plan.collisions:
        print(
            "\nERROR: slug collisions detected. Add exclusions or ship slug v2 "
            "before writing catalog rows.",
            file=out,
        )
        return 2

    if dry_run:
        print("\nDry-run: database unchanged.", file=out)
        return 0

    with Session(use_engine) as session:
        inserted = insert_candidates_in_transaction(session, plan.candidates)
    print(
        f"\nSeed complete: inserted {inserted} catalog items; "
        f"items table expected total {plan.existing_total + inserted}.",
        file=out,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help=f"Path to watchlist YAML (default: {DEFAULT_WATCHLIST_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of ranked catalog names to seed. Default {DEFAULT_LIMIT}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and collision report without writing DB rows.",
    )
    args = parser.parse_args(argv)

    try:
        return run(
            watchlist_path=args.watchlist,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
