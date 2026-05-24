"""Featured-tier seeder for the three-tier watchlist.

Reads per-item Pricempire metadata (rank), filters out current
curated-tier items and configured exclusions, then writes the
top-N-by-rank set into ``data/watchlist.yaml`` as featured-tier entries.

Usage:
    uv run python -m scripts.seed_featured_tier
    uv run python -m scripts.seed_featured_tier --dry-run
    uv run python -m scripts.seed_featured_tier --target-size 200
    uv run python -m scripts.seed_featured_tier --watchlist /path/to/yaml

Workflow:
    1. Run the script. Summary report prints to stdout BEFORE the YAML
       is written, so the planned change is visible.
    2. Review with ``git diff data/watchlist.yaml``.
    3. Commit if happy; ``git checkout -- data/watchlist.yaml`` otherwise.
    4. Deploy triggers ``scripts/seed_watchlist.py`` which inserts the
       new featured-tier items into the ``items`` table.

Idempotent: re-running against the same metadata + exclusions produces
no diff in ``data/watchlist.yaml``.

Curated tier is never touched. Maintain it by hand via
``scripts/watchlist_edit.py`` and direct YAML edits. The featured tier
is the popularity-driven coverage layer; curated is editorial.

Curated-to-featured flow: items dropped from the curated tier (e.g.
after a curated-list refresh) that still rank in the top-N will get
promoted to featured tier by this seeder. That's expected: drop from
curated is editorial, promotion to featured is popularity. The summary report's
"Re-added" section makes the promotion visible. Use
``featured_tier_exclusions:`` in the YAML to veto specific items.

One-time formatting reflow caveat: ruamel.yaml's round-trip preserves
content but normalizes flow-style entry spacing (e.g. drops the
space after ``{`` and before ``}`` in ``- { key: val }`` flow maps).
The first time this seeder runs against a watchlist YAML that was
hand-edited or produced by a previous tool, every existing flow-style
item line will get reformatted to ruamel's canonical style. That's a
one-shot diff on first run; subsequent runs against the seeder-owned
output are byte-clean. To keep content diffs reviewable, run the
seeder once on the current YAML in a separate normalize-only
commit before doing any content-changing run. The semantic invariant
("curated-tier item content is preserved") holds across the reflow.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.connection import get_engine

# Reusing the comment-preserving ruamel.yaml factory + flow-style entry
# helper from watchlist_edit. Internal coupling kept tight on purpose —
# both scripts write the same file with the same formatting rules.
from scripts.watchlist_edit import _flow_entry, _make_yaml

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WATCHLIST_PATH = _REPO_ROOT / "data" / "watchlist.yaml"
DEFAULT_TARGET_SIZE = 500

# How many representative names to print per category in the summary
# report. Large watchlist diffs are reviewed in git; the
# samples make the report scannable while leaving the full set for the
# git diff.
_SAMPLE_SIZE_IN_REPORT = 5

# How many rank-change rows to print in the summary. Top-N by absolute
# rank delta, sorted by |Δ| desc.
_TOP_RANK_CHANGES = 10


# ──────────────────────────────────────────────────────────────────────
# Data shapes (immutable; passed across pure functions)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetadataRow:
    """One rank+liquidity row from the DB ``pricempire_item_metadata``
    table, joined to ``items`` for the market_hash_name lookup.

    ``item_id`` is ``Optional[UUID]`` for forward-compat with code paths
    that construct ``MetadataRow`` without an items-table UUID (e.g.,
    fixture-builders, or hypothetical future feeders that haven't been
    seeded yet). Downstream pipeline never reads ``item_id`` after
    construction — the rest of the module keys on ``market_hash_name``
    end-to-end — so allowing ``None`` is a pure relaxation of the type
    contract. ``TestComputeFeaturedTierWithNullItemId`` pins this invariant
    against future refactors that might accidentally start reading the
    field.
    """

    item_id: uuid.UUID | None
    market_hash_name: str
    rank: int
    liquidity: Decimal | None


@dataclass(frozen=True)
class FeaturedTierCandidate:
    """A market_hash_name that qualifies for the featured tier under the
    current rank + exclusion + curated-set filters."""

    market_hash_name: str
    rank: int
    is_stattrak: bool
    is_souvenir: bool


@dataclass(frozen=True)
class DiffReport:
    """Summary of what the seeder is about to change in
    ``data/watchlist.yaml``.

    - ``added``: items in the new featured tier that were NOT in the
      items table previously. Genuinely new to the watchlist.
    - ``re_added``: items in the new featured tier that DO exist in
      ``items`` but aren't currently in the YAML. This includes curated
      items that were intentionally dropped and later qualify by rank.
    - ``dropped``: items currently in featured tier no longer in the new
      composition (rank fell out of top-N or got added to exclusions).
    - ``kept``: items in both current and new featured tier. Used for
      total composition math and as the source for rank_changes.
    - ``rank_changes``: top-N kept items by |Δrank|. Compares latest
      vs second-latest rank in ``pricempire_item_metadata``. Capped at
      ``_TOP_RANK_CHANGES`` rows.
    - ``exclusion_hits``: items in the exclusion list whose latest
      rank is at or better than the cutoff rank of the new featured
      tier — i.e. exclusions doing operational work. Surfaces
      vestigial exclusions vs. active ones.
    """

    added: list[FeaturedTierCandidate]
    re_added: list[FeaturedTierCandidate]
    dropped: list[str]
    kept: list[str]
    rank_changes: list[tuple[str, int, int]]  # (name, old_rank, new_rank)
    exclusion_hits: list[tuple[str, int]]  # (name, current_rank)


# ──────────────────────────────────────────────────────────────────────
# DB-bound read helpers
# ──────────────────────────────────────────────────────────────────────


def load_latest_metadata(session: Session) -> list[MetadataRow]:
    """Return the latest metadata row per item, sorted by rank ASC.

    Uses ``DISTINCT ON (item_id) ... ORDER BY item_id, timestamp DESC``
    so that items whose rank hasn't changed recently (dedup-suppressed
    writes per ADR 020) are still represented at their current rank.
    A naive ``MAX(timestamp)`` approach would miss them.
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (m.item_id)
                m.item_id,
                i.market_hash_name,
                m.rank,
                m.liquidity
            FROM pricempire_item_metadata m
            JOIN items i ON i.id = m.item_id
            WHERE m.rank IS NOT NULL
            ORDER BY m.item_id, m.timestamp DESC
            """
        )
    ).all()
    return sorted(
        [
            MetadataRow(
                item_id=r.item_id,
                market_hash_name=r.market_hash_name,
                rank=r.rank,
                liquidity=r.liquidity,
            )
            for r in rows
        ],
        key=lambda m: m.rank,
    )


def load_previous_ranks(session: Session) -> dict[str, int]:
    """For each item with at least two metadata rows, return the
    second-most-recent rank. Used to compute the rank_changes section
    of the summary report against the current rank.
    """
    rows = session.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    i.market_hash_name,
                    m.rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY m.item_id
                        ORDER BY m.timestamp DESC
                    ) AS rn
                FROM pricempire_item_metadata m
                JOIN items i ON i.id = m.item_id
                WHERE m.rank IS NOT NULL
            )
            SELECT market_hash_name, rank
            FROM ranked
            WHERE rn = 2
            """
        )
    ).all()
    return {r.market_hash_name: r.rank for r in rows}


def load_existing_item_names(session: Session) -> set[str]:
    """All ``market_hash_name`` values in the ``items`` table.

    Used to distinguish "added (new to watchlist + items table)" from
    "re_added (in items but not currently in YAML)" in the report.
    """
    rows = session.execute(text("SELECT market_hash_name FROM items")).all()
    return {r.market_hash_name for r in rows}


# ──────────────────────────────────────────────────────────────────────
# Pure logic
# ──────────────────────────────────────────────────────────────────────


def detect_flags(market_hash_name: str) -> tuple[bool, bool]:
    """Return ``(is_stattrak, is_souvenir)`` based on the canonical
    market_hash_name prefixes. Pure string match — no DB lookup.

    The prefixes Pricempire's catalog uses match Steam's canonical
    forms exactly:
        StatTrak™ <weapon> | <skin> (<wear>)
        Souvenir <weapon> | <skin> (<wear>)
    """
    return (
        market_hash_name.startswith("StatTrak™ "),
        market_hash_name.startswith("Souvenir "),
    )


def compute_featured_tier(
    metadata_rows: list[MetadataRow],
    curated_set: set[str],
    exclusions: set[str],
    target_size: int,
) -> list[FeaturedTierCandidate]:
    """Pick the top-``target_size``-by-rank market_hash_names for the
    featured tier, skipping curated-set members and exclusions.

    ``target_size`` is the OUTPUT size after filters apply, not the
    input window. Iterates rank-ascending, accumulating candidates
    until result reaches ``target_size``; curated-set members and
    exclusion members are skipped silently along the way. This means
    the featured tier maintains its target size as the exclusion list
    grows — adding an exclusion shifts the cutoff rank one notch
    lower, it doesn't shrink the tier.

    ``test_exclusions_dont_shrink_output`` pins this invariant.
    """
    result: list[FeaturedTierCandidate] = []
    for row in metadata_rows:
        if len(result) >= target_size:
            break
        if row.market_hash_name in curated_set:
            continue
        if row.market_hash_name in exclusions:
            continue
        stt, sv = detect_flags(row.market_hash_name)
        result.append(
            FeaturedTierCandidate(
                market_hash_name=row.market_hash_name,
                rank=row.rank,
                is_stattrak=stt,
                is_souvenir=sv,
            )
        )
    return result


def diff_against_current(
    *,
    new_featured: list[FeaturedTierCandidate],
    current_featured_set: set[str],
    existing_items: set[str],
    metadata_rows: list[MetadataRow],
    previous_ranks: dict[str, int],
    exclusions: set[str],
) -> DiffReport:
    """Compute the added/re_added/dropped/kept/rank_changes/
    exclusion_hits structure relative to the current YAML state.

    Pure function: takes everything it needs as inputs, no DB or
    filesystem access.
    """
    new_names = {c.market_hash_name for c in new_featured}
    new_by_name = {c.market_hash_name: c for c in new_featured}

    added: list[FeaturedTierCandidate] = []
    re_added: list[FeaturedTierCandidate] = []
    for c in new_featured:
        if c.market_hash_name in current_featured_set:
            continue  # kept (rank may have changed; handled below)
        if c.market_hash_name in existing_items:
            re_added.append(c)
        else:
            added.append(c)

    dropped = sorted(current_featured_set - new_names)
    kept = sorted(current_featured_set & new_names)

    # Rank changes among kept items: compare current rank (from
    # new_featured's entry) to previous-cycle rank (from previous_ranks).
    rank_changes_all: list[tuple[str, int, int]] = []
    for name in kept:
        new_rank = new_by_name[name].rank
        old_rank = previous_ranks.get(name)
        if old_rank is None or old_rank == new_rank:
            continue
        rank_changes_all.append((name, old_rank, new_rank))
    rank_changes_all.sort(key=lambda t: -abs(t[2] - t[1]))
    rank_changes = rank_changes_all[:_TOP_RANK_CHANGES]

    # Exclusion hits: items in exclusions whose latest rank is at or
    # better than the cutoff rank of the new featured tier. Approximation:
    # the cutoff is new_featured[-1].rank. If an excluded item's rank is
    # ≤ that, lifting the exclusion would put it in (modulo
    # tie-breaking, which is acceptable for a visibility report).
    if new_featured:
        cutoff_rank = new_featured[-1].rank
        metadata_by_name = {r.market_hash_name: r.rank for r in metadata_rows}
        hits = [
            (name, metadata_by_name[name])
            for name in exclusions
            if name in metadata_by_name and metadata_by_name[name] <= cutoff_rank
        ]
        hits.sort(key=lambda t: t[1])
        exclusion_hits = hits
    else:
        exclusion_hits = []

    return DiffReport(
        added=added,
        re_added=re_added,
        dropped=dropped,
        kept=kept,
        rank_changes=rank_changes,
        exclusion_hits=exclusion_hits,
    )


# ──────────────────────────────────────────────────────────────────────
# YAML state extraction
# ──────────────────────────────────────────────────────────────────────


def partition_yaml_items(yaml_data: Any) -> tuple[set[str], set[str]]:
    """Return ``(curated_set, featured_set)`` of market_hash_names from the
    YAML's ``items:`` block. Items lacking a ``tier:`` field are
    skipped silently — the loader fail-fasts on those at load time,
    so this defensive skip is belt-and-braces."""
    curated_set: set[str] = set()
    featured_set: set[str] = set()
    for item in yaml_data.get("items") or []:
        name = item.get("market_hash_name") if isinstance(item, dict) else None
        tier = item.get("tier") if isinstance(item, dict) else None
        if not name or tier is None:
            continue
        if tier == "curated":
            curated_set.add(name)
        elif tier == "featured":
            featured_set.add(name)
    return curated_set, featured_set


def load_exclusions(yaml_data: Any) -> set[str]:
    """Read the top-level ``featured_tier_exclusions:`` list. Absent or
    empty → empty set. Non-list values raise ValueError."""
    raw = yaml_data.get("featured_tier_exclusions")
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ValueError(
            f"featured_tier_exclusions must be a list, got {type(raw).__name__}"
        )
    return set(raw)


# ──────────────────────────────────────────────────────────────────────
# YAML mutation
# ──────────────────────────────────────────────────────────────────────


def apply_yaml_changes(
    yaml_data: Any,
    diff: DiffReport,
) -> None:
    """Mutate ``yaml_data`` in place: remove dropped items, append
    added + re_added items with ``tier: featured`` (and StatTrak / Souvenir
    flags when applicable). Curated-tier items are never touched.

    Existing featured-tier items that survive (kept) are left unchanged —
    we don't rewrite them to update rank in the YAML because rank is
    NOT stored in the YAML. The seeder is composition-only; rank lives
    in ``pricempire_item_metadata``.
    """
    items = yaml_data.get("items")
    if items is None:
        raise ValueError("yaml_data has no items: key — refusing to mutate")

    # Drops: filter out items whose market_hash_name appears in dropped.
    dropped_set = set(diff.dropped)
    if dropped_set:
        # Build a fresh sequence preserving ruamel comment placement.
        # CommentedSeq supports item-wise deletion; iterate indices in
        # reverse so deletions don't shift later indices.
        for idx in reversed(range(len(items))):
            entry = items[idx]
            name = entry.get("market_hash_name") if isinstance(entry, dict) else None
            if name in dropped_set:
                del items[idx]

    # Adds + re-adds: append minimal flow-style entries with
    # tier: featured. We don't fill item_type/weapon_name/skin_name/wear
    # for featured-tier items; those columns are nullable in items table
    # and the bot's category dispatch falls back to display-name
    # parsing.
    for candidate in [*diff.re_added, *diff.added]:
        fields: dict = {
            "market_hash_name": candidate.market_hash_name,
        }
        if candidate.is_stattrak:
            fields["is_stattrak"] = True
        if candidate.is_souvenir:
            fields["is_souvenir"] = True
        fields["tier"] = "featured"
        items.append(_flow_entry(fields))


def write_yaml(path: Path, yaml_data: Any) -> None:
    """Persist ``yaml_data`` to ``path`` via the comment-preserving
    ruamel.yaml factory from scripts.watchlist_edit. Two passes: dump
    to a tempfile, then atomic rename. Not for safety against power
    loss (the script writes a single user-facing file with manual
    review); for safety against partial-write during a crash."""
    yaml = _make_yaml()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(yaml_data, f)
    tmp_path.replace(path)


# ──────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────


def _format_sample(names: list[str], cap: int = _SAMPLE_SIZE_IN_REPORT) -> str:
    """``"X, Y, Z, ..."`` capped at ``cap`` entries with an ellipsis if
    the list is longer."""
    if not names:
        return "(none)"
    head = names[:cap]
    rendered = ", ".join(head)
    if len(names) > cap:
        rendered += f", … (+{len(names) - cap} more)"
    return rendered


def print_summary(
    diff: DiffReport,
    *,
    target_size: int,
    total_composition: int,
    file: TextIO | None = None,
) -> None:
    """Render the review report to ``file`` (default stdout).

    Format is deliberately plain text: no tables, no color, no
    Unicode beyond what market_hash_names already carry. Greppable
    from terminal history.
    """
    out = file if file is not None else sys.stdout

    print("Featured-tier seed plan:", file=out)
    print(
        f"  Target size: {target_size}; new composition: "
        f"{total_composition} items",
        file=out,
    )

    print(
        f"  Added (new to watchlist): {len(diff.added)}", file=out
    )
    if diff.added:
        sample = [f"{c.market_hash_name} (rank {c.rank})" for c in diff.added]
        print(f"    {_format_sample(sample)}", file=out)

    print(
        f"  Re-added (previously tracked, now restored to featured): "
        f"{len(diff.re_added)}",
        file=out,
    )
    if diff.re_added:
        sample = [
            f"{c.market_hash_name} (rank {c.rank})" for c in diff.re_added
        ]
        print(f"    {_format_sample(sample)}", file=out)

    print(f"  Dropped from featured: {len(diff.dropped)}", file=out)
    if diff.dropped:
        print(f"    {_format_sample(diff.dropped)}", file=out)

    print(f"  Kept in featured: {len(diff.kept)}", file=out)

    print(
        f"  Top rank changes among kept (cap {_TOP_RANK_CHANGES}): "
        f"{len(diff.rank_changes)}",
        file=out,
    )
    for name, old_rank, new_rank in diff.rank_changes:
        delta = new_rank - old_rank
        arrow = f"{old_rank} -> {new_rank}"
        sign = "+" if delta > 0 else ""  # delta is signed
        print(f"    {name}: {arrow} ({sign}{delta})", file=out)

    print(
        f"  Exclusion-list hits (excluded items that would have "
        f"qualified): {len(diff.exclusion_hits)}",
        file=out,
    )
    if diff.exclusion_hits:
        sample = [
            f"{name} (rank {rank})" for name, rank in diff.exclusion_hits
        ]
        print(f"    {_format_sample(sample)}", file=out)


# ──────────────────────────────────────────────────────────────────────
# Top-level orchestrator
# ──────────────────────────────────────────────────────────────────────


def run(
    *,
    watchlist_path: Path,
    target_size: int,
    dry_run: bool,
    file: TextIO | None = None,
) -> int:
    """End-to-end: load YAML + metadata, compute new featured tier,
    print report, optionally write YAML. Returns 0 on success.

    ``file`` is the report's output stream — defaults to stdout; tests
    inject a StringIO. The seeder always prints before writing, so
    even when ``dry_run=False`` the plan is visible first.
    """
    out = file if file is not None else sys.stdout

    yaml = _make_yaml()
    with watchlist_path.open("r", encoding="utf-8") as f:
        yaml_data = yaml.load(f)

    if yaml_data is None:
        print(
            f"ERROR: {watchlist_path} is empty or unparseable",
            file=sys.stderr,
        )
        return 2

    curated_set, current_featured_set = partition_yaml_items(yaml_data)
    exclusions = load_exclusions(yaml_data)

    engine = get_engine()
    with Session(engine) as session:
        metadata_rows = load_latest_metadata(session)
        previous_ranks = load_previous_ranks(session)
        existing_items = load_existing_item_names(session)

    new_featured = compute_featured_tier(
        metadata_rows=metadata_rows,
        curated_set=curated_set,
        exclusions=exclusions,
        target_size=target_size,
    )
    diff = diff_against_current(
        new_featured=new_featured,
        current_featured_set=current_featured_set,
        existing_items=existing_items,
        metadata_rows=metadata_rows,
        previous_ranks=previous_ranks,
        exclusions=exclusions,
    )
    print_summary(
        diff,
        target_size=target_size,
        total_composition=len(new_featured),
        file=out,
    )

    if dry_run:
        print(f"\nDry-run: {watchlist_path} unchanged.", file=out)
        return 0

    apply_yaml_changes(yaml_data, diff)
    write_yaml(watchlist_path, yaml_data)
    print(
        f"\nWrote {len(new_featured)} featured-tier items to {watchlist_path}. "
        f"Review with `git diff {watchlist_path.name}`.",
        file=out,
    )
    return 0


# ──────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
        help=f"Path to watchlist YAML (default: {DEFAULT_WATCHLIST_PATH})",
    )
    parser.add_argument(
        "--target-size",
        type=int,
        default=DEFAULT_TARGET_SIZE,
        help=(
            f"Number of featured-tier items in the output, after filters. "
            f"Default {DEFAULT_TARGET_SIZE}."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the summary report but do NOT write YAML. The "
            "default invocation writes YAML; review with `git diff` "
            "afterward."
        ),
    )
    args = parser.parse_args(argv)

    if args.target_size < 0:
        print(
            f"ERROR: --target-size must be non-negative, got "
            f"{args.target_size}",
            file=sys.stderr,
        )
        return 2
    if not args.watchlist.exists():
        print(
            f"ERROR: watchlist not found at {args.watchlist}",
            file=sys.stderr,
        )
        return 2

    return run(
        watchlist_path=args.watchlist,
        target_size=args.target_size,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
