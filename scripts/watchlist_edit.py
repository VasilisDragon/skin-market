"""CLI for managing ``data/watchlist.yaml`` and the corresponding ``items`` table.

Three subcommands:

- ``add``     append an item to the YAML, then seed the new row into the DB.
- ``remove``  delete an item from the YAML AND the DB. Refuses if the item
              has any ``prices`` or ``insights`` rows in the DB, unless
              ``--force`` is passed (in which case those rows are deleted too).
- ``list``    print the current watchlist. Optional ``--type`` filter.

Examples::

    uv run python -m scripts.watchlist_edit list
    uv run python -m scripts.watchlist_edit list --type knife
    uv run python -m scripts.watchlist_edit add \\
        --name "Five-SeveN | Hyper Beast (Field-Tested)" \\
        --type pistol --weapon "Five-SeveN" --skin "Hyper Beast" --wear "Field-Tested"
    uv run python -m scripts.watchlist_edit remove --name "Five-SeveN | Hyper Beast (Field-Tested)"
    uv run python -m scripts.watchlist_edit remove --name "..." --force  # drops collected data

YAML edits go through ``ruamel.yaml`` so the file's header comments and
section markers survive each rewrite. New entries are emitted in flow
style to match the existing items' layout. The ``items`` table is
updated by re-running ``scripts.seed_watchlist.seed`` (idempotent
ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.models import Item
from db.naming import normalize_name
from scripts.seed_watchlist import DEFAULT_WATCHLIST_PATH, seed

# CS2 weapon classes the watchlist supports. The seed itself accepts any
# string; this is a tighter constraint to surface typos early.
VALID_TYPES = frozenset(
    {"rifle", "sniper", "pistol", "knife", "glove", "smg", "shotgun", "machinegun"}
)


def _make_yaml() -> YAML:
    """Build a ruamel.yaml instance configured to round-trip our watchlist
    file with minimum diff churn."""
    y = YAML()
    y.preserve_quotes = True
    # Match the existing data/watchlist.yaml indentation.
    y.indent(mapping=2, sequence=4, offset=2)
    # Items can have long single-line flow entries; don't auto-wrap them.
    y.width = 200
    return y


def _load(path: Path) -> dict:
    yaml = _make_yaml()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None or "items" not in data or "sources" not in data:
        raise ValueError(
            f"{path}: missing required top-level keys 'items' and 'sources'"
        )
    return data


def _save(path: Path, data) -> None:
    yaml = _make_yaml()
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)


def _flow_entry(fields: dict) -> CommentedMap:
    """Build a CommentedMap that ruamel dumps in flow ``{k: v, ...}`` style,
    matching the layout of items already in ``data/watchlist.yaml``."""
    entry = CommentedMap(fields)
    entry.fa.set_flow_style()
    return entry


# --- subcommands ---


def cmd_add(args: argparse.Namespace) -> int:
    if args.type not in VALID_TYPES:
        print(
            f"Error: --type must be one of {sorted(VALID_TYPES)}, "
            f"got {args.type!r}",
            file=sys.stderr,
        )
        return 2

    name = normalize_name(args.name)
    data = _load(args.watchlist)

    existing = {normalize_name(it["market_hash_name"]) for it in data["items"]}
    if name in existing:
        print(f"Item {name!r} is already in {args.watchlist}; nothing to do.")
        return 1

    fields: dict = {
        "market_hash_name": name,
        "item_type": args.type,
        "weapon_name": args.weapon,
        "skin_name": args.skin,
        "wear": args.wear,
    }
    if args.is_stattrak:
        fields["is_stattrak"] = True
    if args.is_souvenir:
        fields["is_souvenir"] = True
    # Manual additions default to curated tier. The featured tier is
    # populated separately by scripts/seed_featured_tier.py.
    fields["tier"] = "curated"

    data["items"].append(_flow_entry(fields))
    _save(args.watchlist, data)

    # Idempotent ON CONFLICT DO NOTHING; inserts only the new item.
    items_count, _sources_count = seed(args.watchlist)
    print(f"Added {name!r}. Watchlist now has {items_count} items in the DB.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    name = normalize_name(args.name)
    data = _load(args.watchlist)

    idx = None
    for i, it in enumerate(data["items"]):
        if normalize_name(it["market_hash_name"]) == name:
            idx = i
            break
    if idx is None:
        print(
            f"Item {name!r} is not in {args.watchlist}; nothing to remove.",
            file=sys.stderr,
        )
        return 1

    # Inspect DB state before touching anything. The FK is restrictive
    # (no ON DELETE CASCADE on prices.item_id / insights.item_id), so the
    # script must explicitly delete dependent rows when --force is set.
    engine = get_engine()
    with Session(engine) as session:
        item = session.execute(
            select(Item).where(Item.market_hash_name == name)
        ).scalar_one_or_none()

        if item is None:
            prices_count = 0
            insights_count = 0
        else:
            prices_count = int(
                session.execute(
                    text("SELECT COUNT(*) FROM prices WHERE item_id = :i"),
                    {"i": item.id},
                ).scalar_one()
            )
            insights_count = int(
                session.execute(
                    text("SELECT COUNT(*) FROM insights WHERE item_id = :i"),
                    {"i": item.id},
                ).scalar_one()
            )

        if (prices_count > 0 or insights_count > 0) and not args.force:
            print(
                f"Refusing to remove {name!r}: {prices_count} prices rows "
                f"and {insights_count} insights rows would be deleted. "
                f"Pass --force to proceed.",
                file=sys.stderr,
            )
            return 1

        # Delete dependent rows + the item itself. We do this BEFORE
        # rewriting the YAML so a DB failure leaves both sides intact.
        if item is not None:
            if prices_count > 0:
                session.execute(
                    text("DELETE FROM prices WHERE item_id = :i"),
                    {"i": item.id},
                )
            if insights_count > 0:
                session.execute(
                    text("DELETE FROM insights WHERE item_id = :i"),
                    {"i": item.id},
                )
            session.delete(item)
            session.commit()

    # DB write succeeded — now rewrite the YAML.
    del data["items"][idx]
    _save(args.watchlist, data)

    parts = [f"Removed {name!r}."]
    if prices_count > 0:
        parts.append(f"Deleted {prices_count} prices rows.")
    if insights_count > 0:
        parts.append(f"Deleted {insights_count} insights rows.")
    print(" ".join(parts))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    data = _load(args.watchlist)
    items = list(data["items"])

    if args.type:
        items = [it for it in items if it.get("item_type") == args.type]

    if not items:
        suffix = f" for item_type={args.type!r}" if args.type else ""
        print(f"No items found{suffix}.")
        return 0

    items.sort(
        key=lambda it: (it.get("item_type", ""), it["market_hash_name"])
    )
    print(f"{'TYPE':10} {'WEAR':18} ITEM")
    print("-" * 100)
    for it in items:
        print(
            f"{it.get('item_type', ''):10} "
            f"{it.get('wear', ''):18} "
            f"{it['market_hash_name']}"
        )
    print(f"\n{len(items)} item(s).")
    return 0


# --- argument parsing ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], prog="watchlist_edit"
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help=f"path to watchlist YAML (default: {DEFAULT_WATCHLIST_PATH})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="append an item to the watchlist")
    p_add.add_argument("--name", required=True, help="market_hash_name (exact)")
    p_add.add_argument(
        "--type",
        required=True,
        help=f"one of {sorted(VALID_TYPES)}",
    )
    p_add.add_argument("--weapon", required=True, help="weapon_name")
    p_add.add_argument("--skin", required=True, help="skin_name")
    p_add.add_argument(
        "--wear",
        required=True,
        help='e.g. "Factory New", "Field-Tested"',
    )
    p_add.add_argument(
        "--is-stattrak", action="store_true", help="mark as StatTrak™"
    )
    p_add.add_argument(
        "--is-souvenir", action="store_true", help="mark as Souvenir"
    )
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser(
        "remove", help="remove an item from the watchlist and DB"
    )
    p_remove.add_argument("--name", required=True, help="market_hash_name (exact)")
    p_remove.add_argument(
        "--force",
        action="store_true",
        help="also delete the item's prices and insights rows",
    )
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="print the current watchlist")
    p_list.add_argument(
        "--type", help="filter by item_type (e.g. rifle, sniper, knife)"
    )
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
