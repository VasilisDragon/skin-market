"""Tests for scripts/seed_broad_tier.py.

Most tests are pure-logic (no DB) and run on any host. The DB-required
suite at the bottom is gated by the same _db_required skip pattern the
other collector tests use.

What these cover:

- compute_broad_tier: top-N selection, deep precedence, exclusion
  handling, the "exclusions don't shrink output" invariant (per Step 1
  clarification).
- detect_flags: StatTrak™ / Souvenir prefix detection.
- diff_against_current: added vs re_added split (in-items-but-not-YAML),
  dropped, kept, rank changes (top-N by |Δ|), exclusion hits.
- print_summary: each section appears with counts; dry-run preserves
  the YAML byte-for-byte (per the Step 3 clarification — byte-equality,
  not mtime).
- YAML write path: deep tier never touched, comments preserved,
  StatTrak/Souvenir flags set when appropriate.
- Idempotency: second run produces zero diff.
- CLI: --dry-run, --target-size, bogus paths.
"""

from __future__ import annotations

import io
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from db.connection import get_engine
from scripts import seed_broad_tier
from scripts.seed_broad_tier import (
    BroadTierCandidate,
    DiffReport,
    MetadataRow,
    compute_broad_tier,
    detect_flags,
    diff_against_current,
    load_exclusions,
    partition_yaml_items,
    print_summary,
)
from scripts.seed_broad_tier import (
    main as seed_main,
)
from scripts.seed_broad_tier import (
    run as seed_run,
)


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        return False


_db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


# ──────────────────────────────────────────────────────────────────────
# Helpers for building fixture data
# ──────────────────────────────────────────────────────────────────────


def _meta(rank: int, name: str | None = None) -> MetadataRow:
    """Build a synthetic MetadataRow for a given rank."""
    return MetadataRow(
        item_id=uuid.uuid4(),
        market_hash_name=name or f"Item rank {rank} (Field-Tested)",
        rank=rank,
        liquidity=Decimal("50.00"),
    )


# A minimal YAML body the seeder can read end-to-end. Two deep items,
# two broad items, two exclusions, schema_version 2.
_MIN_YAML = """\
schema_version: 2

broad_tier_exclusions:
  - "Excluded Skin A (Factory New)"
  - "Excluded Skin B (Battle-Scarred)"

sources:
  - { name: skinport, base_url: https://example, rate_limit_per_minute: 60, enabled: true }

items:
  - market_hash_name: "AK-47 | Redline (Field-Tested)"
    item_type: rifle
    weapon_name: "AK-47"
    skin_name: "Redline"
    wear: "Field-Tested"
    tier: deep
  - market_hash_name: "M4A4 | Howl (Factory New)"
    item_type: rifle
    weapon_name: "M4A4"
    skin_name: "Howl"
    wear: "Factory New"
    tier: deep
  - { market_hash_name: "Currently Broad A (Field-Tested)", tier: broad }
  - { market_hash_name: "Currently Broad B (Minimal Wear)", tier: broad }
"""


# ──────────────────────────────────────────────────────────────────────
# detect_flags — pure prefix detection
# ──────────────────────────────────────────────────────────────────────


class TestDetectFlags:
    def test_default_flags_false(self) -> None:
        assert detect_flags("AK-47 | Redline (Field-Tested)") == (False, False)

    def test_detects_stattrak_prefix(self) -> None:
        name = "StatTrak™ AK-47 | Redline (Field-Tested)"
        assert detect_flags(name) == (True, False)

    def test_detects_souvenir_prefix(self) -> None:
        name = "Souvenir AWP | Dragon Lore (Battle-Scarred)"
        assert detect_flags(name) == (False, True)

    def test_souvenir_and_stattrak_mutually_exclusive_in_practice(
        self,
    ) -> None:
        """The function reports both flags from the name verbatim; if
        someone constructs a 'StatTrak™ Souvenir ...' string (which
        doesn't exist in real CS2 inventory), neither prefix wins —
        the StatTrak™ prefix doesn't match and the Souvenir prefix
        doesn't match because StatTrak™ comes first."""
        # No real CS2 item carries both. The detector just keys off
        # the leading prefix, so the realistic "StatTrak™ X" answers
        # (True, False) — the leading 'StatTrak™ ' doesn't equal
        # 'Souvenir ', so the souvenir check is False.
        name = "StatTrak™ Karambit | Doppler (Factory New)"
        assert detect_flags(name) == (True, False)


# ──────────────────────────────────────────────────────────────────────
# compute_broad_tier — composition logic
# ──────────────────────────────────────────────────────────────────────


class TestComputeBroadTier:
    def test_takes_top_n_by_rank(self) -> None:
        rows = [_meta(i) for i in range(1, 11)]  # ranks 1-10
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=set(),
            target_size=5,
        )
        assert [c.rank for c in result] == [1, 2, 3, 4, 5]

    def test_skips_deep_tier_items(self) -> None:
        rows = [_meta(i) for i in range(1, 11)]
        # The rank-1 item is also in deep tier; broad must skip it.
        deep_set = {rows[0].market_hash_name}
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=deep_set,
            exclusions=set(),
            target_size=5,
        )
        assert [c.rank for c in result] == [2, 3, 4, 5, 6]

    def test_skips_exclusion_list(self) -> None:
        rows = [_meta(i) for i in range(1, 11)]
        exclusions = {rows[2].market_hash_name}  # rank 3
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=exclusions,
            target_size=5,
        )
        assert [c.rank for c in result] == [1, 2, 4, 5, 6]

    def test_exclusions_dont_shrink_output(self) -> None:
        """Pin the load-bearing invariant: target_size is the OUTPUT
        size after filters apply, not the input window. Two
        exclusions in the top-10 should still yield 5 items in the
        broad tier (using ranks 1, 2, 4, 6, 7 — skipping 3 and 5).
        """
        rows = [_meta(i) for i in range(1, 11)]
        exclusions = {
            rows[2].market_hash_name,  # rank 3
            rows[4].market_hash_name,  # rank 5
        }
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=exclusions,
            target_size=5,
        )
        assert len(result) == 5, (
            f"target_size=5 must produce 5 items even with 2 exclusions; "
            f"got {len(result)}: {[c.rank for c in result]}"
        )
        assert [c.rank for c in result] == [1, 2, 4, 6, 7]

    def test_skips_both_deep_and_exclusions(self) -> None:
        rows = [_meta(i) for i in range(1, 11)]
        deep_set = {rows[1].market_hash_name}  # rank 2
        exclusions = {rows[3].market_hash_name}  # rank 4
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=deep_set,
            exclusions=exclusions,
            target_size=5,
        )
        assert [c.rank for c in result] == [1, 3, 5, 6, 7]

    def test_target_size_respected_when_input_is_larger(self) -> None:
        rows = [_meta(i) for i in range(1, 1001)]
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=set(),
            target_size=500,
        )
        assert len(result) == 500
        assert result[0].rank == 1
        assert result[-1].rank == 500

    def test_empty_metadata_returns_empty(self) -> None:
        assert (
            compute_broad_tier(
                metadata_rows=[],
                deep_set=set(),
                exclusions=set(),
                target_size=500,
            )
            == []
        )

    def test_input_smaller_than_target_returns_all(self) -> None:
        rows = [_meta(i) for i in range(1, 4)]  # 3 rows
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=set(),
            target_size=500,
        )
        assert len(result) == 3

    def test_stattrak_flag_set_on_candidate(self) -> None:
        rows = [
            MetadataRow(
                item_id=uuid.uuid4(),
                market_hash_name="StatTrak™ AK-47 | Redline (Field-Tested)",
                rank=1,
                liquidity=Decimal("80.00"),
            )
        ]
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=set(),
            target_size=1,
        )
        assert result[0].is_stattrak is True
        assert result[0].is_souvenir is False

    def test_souvenir_flag_set_on_candidate(self) -> None:
        rows = [
            MetadataRow(
                item_id=uuid.uuid4(),
                market_hash_name="Souvenir AWP | Dragon Lore (Field-Tested)",
                rank=1,
                liquidity=Decimal("10.00"),
            )
        ]
        result = compute_broad_tier(
            metadata_rows=rows,
            deep_set=set(),
            exclusions=set(),
            target_size=1,
        )
        assert result[0].is_stattrak is False
        assert result[0].is_souvenir is True


# ──────────────────────────────────────────────────────────────────────
# diff_against_current — added/re_added/dropped/kept/rank/excl
# ──────────────────────────────────────────────────────────────────────


def _candidate(name: str, rank: int) -> BroadTierCandidate:
    stt, sv = detect_flags(name)
    return BroadTierCandidate(
        market_hash_name=name,
        rank=rank,
        is_stattrak=stt,
        is_souvenir=sv,
    )


class TestDiffAgainstCurrent:
    def test_marks_additions_new_to_items(self) -> None:
        """Item in new_broad, not in YAML, not in items table → added.
        """
        new = [_candidate("Brand New Item (FN)", 100)]
        report = diff_against_current(
            new_broad=new,
            current_broad_set=set(),
            existing_items=set(),
            metadata_rows=[],
            previous_ranks={},
            exclusions=set(),
        )
        assert [c.market_hash_name for c in report.added] == [
            "Brand New Item (FN)"
        ]
        assert report.re_added == []

    def test_marks_re_added_when_in_items_but_not_yaml(self) -> None:
        """Item in new_broad, not in YAML, but already in items table
        → re_added (the Step-7 deep-tier-drop-flowing-into-broad
        case).
        """
        new = [_candidate("AK-47 | Redline (FT)", 50)]
        report = diff_against_current(
            new_broad=new,
            current_broad_set=set(),
            existing_items={"AK-47 | Redline (FT)"},
            metadata_rows=[],
            previous_ranks={},
            exclusions=set(),
        )
        assert report.added == []
        assert [c.market_hash_name for c in report.re_added] == [
            "AK-47 | Redline (FT)"
        ]

    def test_marks_drops(self) -> None:
        new = [_candidate("Kept Item (FN)", 5)]
        current = {"Kept Item (FN)", "Dropped Item (FN)"}
        report = diff_against_current(
            new_broad=new,
            current_broad_set=current,
            existing_items=current,
            metadata_rows=[],
            previous_ranks={},
            exclusions=set(),
        )
        assert report.dropped == ["Dropped Item (FN)"]
        assert report.kept == ["Kept Item (FN)"]

    def test_marks_rank_changes_for_kept_items(self) -> None:
        new = [_candidate("Kept Item (FN)", 5)]
        report = diff_against_current(
            new_broad=new,
            current_broad_set={"Kept Item (FN)"},
            existing_items={"Kept Item (FN)"},
            metadata_rows=[],
            previous_ranks={"Kept Item (FN)": 10},
            exclusions=set(),
        )
        assert report.rank_changes == [("Kept Item (FN)", 10, 5)]

    def test_rank_change_zero_delta_excluded(self) -> None:
        """Items whose rank didn't change shouldn't appear in
        rank_changes — only meaningful movement is interesting."""
        new = [_candidate("Steady Item (FN)", 10)]
        report = diff_against_current(
            new_broad=new,
            current_broad_set={"Steady Item (FN)"},
            existing_items={"Steady Item (FN)"},
            metadata_rows=[],
            previous_ranks={"Steady Item (FN)": 10},
            exclusions=set(),
        )
        assert report.rank_changes == []

    def test_top_n_rank_changes_capped(self) -> None:
        """50 items with rank changes → only top 10 by |Δrank| in
        the report, sorted desc by absolute delta."""
        # Build 50 candidates with new ranks 1..50 and previous ranks
        # that produce increasing absolute deltas.
        new = [_candidate(f"Item {i} (FN)", i) for i in range(1, 51)]
        previous_ranks = {f"Item {i} (FN)": i + i for i in range(1, 51)}
        report = diff_against_current(
            new_broad=new,
            current_broad_set={f"Item {i} (FN)" for i in range(1, 51)},
            existing_items={f"Item {i} (FN)" for i in range(1, 51)},
            metadata_rows=[],
            previous_ranks=previous_ranks,
            exclusions=set(),
        )
        assert len(report.rank_changes) == 10
        # Top 10 should be the items with largest |Δ| — items 41..50
        # (delta=41..50). Order: largest |Δ| first.
        names = [r[0] for r in report.rank_changes]
        assert names[0] == "Item 50 (FN)"
        assert names[-1] == "Item 41 (FN)"

    def test_exclusion_hits_includes_excluded_within_cutoff(self) -> None:
        """Excluded item with rank ≤ cutoff (worst rank in new_broad)
        is reported as a hit."""
        meta = [_meta(i) for i in range(1, 11)]
        # New broad takes ranks 1, 2, 3, 4, 6 (skipping rank-5
        # excluded item). Cutoff = 6.
        excluded_name = meta[4].market_hash_name  # rank 5
        new = [
            _candidate(meta[i].market_hash_name, meta[i].rank)
            for i in [0, 1, 2, 3, 5]
        ]
        report = diff_against_current(
            new_broad=new,
            current_broad_set=set(),
            existing_items=set(),
            metadata_rows=meta,
            previous_ranks={},
            exclusions={excluded_name},
        )
        assert report.exclusion_hits == [(excluded_name, 5)]

    def test_exclusion_hits_excludes_below_cutoff(self) -> None:
        """An exclusion whose rank is worse than the cutoff isn't
        doing operational work — not reported as a hit."""
        meta = [_meta(i) for i in range(1, 11)]
        # New broad: ranks 1-5, cutoff = 5. Excluded item is rank 8,
        # outside the cutoff.
        excluded_name = meta[7].market_hash_name  # rank 8
        new = [
            _candidate(meta[i].market_hash_name, meta[i].rank)
            for i in range(5)
        ]
        report = diff_against_current(
            new_broad=new,
            current_broad_set=set(),
            existing_items=set(),
            metadata_rows=meta,
            previous_ranks={},
            exclusions={excluded_name},
        )
        assert report.exclusion_hits == []


# ──────────────────────────────────────────────────────────────────────
# YAML state extraction
# ──────────────────────────────────────────────────────────────────────


class TestYamlPartition:
    def test_partition_yaml_items(self, tmp_path: Path) -> None:
        from scripts.watchlist_edit import _make_yaml

        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)
        with path.open() as f:
            data = _make_yaml().load(f)
        deep, broad = partition_yaml_items(data)
        assert deep == {
            "AK-47 | Redline (Field-Tested)",
            "M4A4 | Howl (Factory New)",
        }
        assert broad == {
            "Currently Broad A (Field-Tested)",
            "Currently Broad B (Minimal Wear)",
        }

    def test_partition_handles_missing_items_section(self) -> None:
        deep, broad = partition_yaml_items({})
        assert deep == set()
        assert broad == set()

    def test_load_exclusions(self, tmp_path: Path) -> None:
        from scripts.watchlist_edit import _make_yaml

        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)
        with path.open() as f:
            data = _make_yaml().load(f)
        assert load_exclusions(data) == {
            "Excluded Skin A (Factory New)",
            "Excluded Skin B (Battle-Scarred)",
        }

    def test_load_exclusions_absent_returns_empty(self) -> None:
        assert load_exclusions({}) == set()

    def test_load_exclusions_handles_explicit_empty_list(
        self, tmp_path: Path
    ) -> None:
        """Phase 2b Step 7.1 ships data/watchlist.yaml with an explicit
        ``broad_tier_exclusions: []`` block (rather than omitting the
        key). Pin that the loader handles the explicit-empty-list case
        as cleanly as the absent-key case — same outcome (empty set,
        no raise). Future YAML edits that subtly break this go
        undetected without this test.
        """
        from scripts.watchlist_edit import _make_yaml

        path = tmp_path / "wl.yaml"
        path.write_text(
            "schema_version: 2\n"
            "broad_tier_exclusions: []\n"
            "sources:\n"
            "  - { name: skinport, base_url: https://example, "
            "rate_limit_per_minute: 60, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "X (FN)", item_type: rifle, '
            'tier: deep }\n',
        )
        with path.open() as f:
            data = _make_yaml().load(f)
        # Explicit empty list parses to a Python list (not None),
        # but load_exclusions normalizes both to set().
        assert data["broad_tier_exclusions"] == []
        assert load_exclusions(data) == set()

    def test_load_exclusions_non_list_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            load_exclusions({"broad_tier_exclusions": "not-a-list"})


# ──────────────────────────────────────────────────────────────────────
# print_summary — output to stream
# ──────────────────────────────────────────────────────────────────────


class TestPrintSummary:
    def test_shows_all_categories(self) -> None:
        diff = DiffReport(
            added=[_candidate("New Item (FN)", 100)],
            re_added=[_candidate("Re-Added Item (FN)", 50)],
            dropped=["Dropped (FN)"],
            kept=["Kept (FN)"],
            rank_changes=[("Kept (FN)", 200, 50)],
            exclusion_hits=[("Excluded (FN)", 30)],
        )
        buf = io.StringIO()
        print_summary(diff, target_size=500, total_composition=2, file=buf)
        out = buf.getvalue()
        assert "Added (new to watchlist): 1" in out
        assert "Re-added (previously tracked, now restored to broad): 1" in out
        assert "Dropped from broad: 1" in out
        assert "Kept in broad: 1" in out
        assert "Kept (FN): 200 -> 50" in out
        assert "Exclusion-list hits" in out
        assert "Excluded (FN)" in out

    def test_zero_count_sections_render_cleanly(self) -> None:
        """An empty section shows ': 0' and no sample line, not '(none)'
        scattered everywhere or KeyError."""
        empty = DiffReport(
            added=[],
            re_added=[],
            dropped=[],
            kept=[],
            rank_changes=[],
            exclusion_hits=[],
        )
        buf = io.StringIO()
        print_summary(empty, target_size=500, total_composition=0, file=buf)
        out = buf.getvalue()
        assert "Added (new to watchlist): 0" in out
        assert "Re-added (previously tracked, now restored to broad): 0" in out
        assert "Dropped from broad: 0" in out
        assert "Kept in broad: 0" in out


# ──────────────────────────────────────────────────────────────────────
# CLI shape and bad-input handling
# ──────────────────────────────────────────────────────────────────────


class TestCli:
    def test_returns_nonzero_on_missing_yaml(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = seed_main(["--watchlist", str(tmp_path / "nope.yaml")])
        captured = capsys.readouterr().err
        assert rc != 0
        assert "not found" in captured

    def test_returns_nonzero_on_negative_target_size(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--target-size 0`` is a legitimate operator request
        ("zero broad-tier items wanted" — used for Step 7.0's
        normalize-only commit). Only NEGATIVE values are rejected."""
        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)
        rc = seed_main(
            ["--watchlist", str(path), "--target-size", "-1"]
        )
        captured = capsys.readouterr().err
        assert rc != 0
        assert "must be non-negative" in captured


# ──────────────────────────────────────────────────────────────────────
# DB-required end-to-end suite
# ──────────────────────────────────────────────────────────────────────


_SENTINEL_PREFIX = "__BroadTierSentinel__"


@pytest.fixture
def sentinel_items():
    """Insert 10 sentinel items into `items` + metadata rows ranking
    them 1-10. Yield a list of (item_id, market_hash_name, rank).
    Cleanup removes the items + their metadata rows.
    """
    if not _db_reachable():
        yield None
        return

    items_data: list[tuple[uuid.UUID, str, int]] = []
    for i in range(1, 11):
        name = f"{_SENTINEL_PREFIX} Item {i} (Field-Tested)"
        items_data.append((uuid.uuid4(), name, i))

    engine = get_engine()
    with Session(engine) as session:
        # Purge any leftover from a prior aborted run.
        session.execute(
            text(
                "DELETE FROM pricempire_item_metadata WHERE item_id IN "
                "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
            ),
            {"pat": f"{_SENTINEL_PREFIX}%"},
        )
        session.execute(
            text("DELETE FROM items WHERE market_hash_name LIKE :pat"),
            {"pat": f"{_SENTINEL_PREFIX}%"},
        )
        for item_id, name, rank in items_data:
            session.execute(
                text(
                    "INSERT INTO items "
                    "(id, market_hash_name, display_name, slug, item_type) "
                    "VALUES (:id, :name, :name, :slug, 'rifle')"
                ),
                {
                    "id": item_id,
                    "name": name,
                    "slug": (
                        f"broad-tier-sentinel-item-{rank}-field-tested"
                    ),
                },
            )
            # One metadata row per item with the synthetic rank.
            session.execute(
                text(
                    "INSERT INTO pricempire_item_metadata "
                    "(item_id, timestamp, rank) "
                    "VALUES (:id, NOW(), :rank)"
                ),
                {"id": item_id, "rank": rank},
            )
        session.commit()

    yield items_data

    with Session(engine) as session:
        session.execute(
            text(
                "DELETE FROM pricempire_item_metadata WHERE item_id IN "
                "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
            ),
            {"pat": f"{_SENTINEL_PREFIX}%"},
        )
        session.execute(
            text("DELETE FROM items WHERE market_hash_name LIKE :pat"),
            {"pat": f"{_SENTINEL_PREFIX}%"},
        )
        session.commit()


class TestRunEndToEnd:
    @_db_required
    def test_dry_run_preserves_yaml_byte_for_byte(
        self, tmp_path: Path, sentinel_items
    ) -> None:
        """Test 22 (per Step 3 clarification): replace mtime check
        with byte-equality. Read YAML bytes pre-run, read post-run,
        assert equal — deterministic regardless of filesystem
        timestamp resolution."""
        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)
        before = path.read_bytes()

        rc = seed_run(
            watchlist_path=path,
            target_size=5,
            dry_run=True,
            file=io.StringIO(),
        )
        assert rc == 0

        after = path.read_bytes()
        assert before == after, (
            "--dry-run wrote to the YAML; the script must not mutate "
            "the file when dry_run=True"
        )

    @_db_required
    def test_writes_top_n_broad_tier_entries(
        self, tmp_path: Path, sentinel_items
    ) -> None:
        """Run against 10 synthetic metadata rows (sentinel ranks
        1-10) PLUS whatever production data the collector has left in
        ``pricempire_item_metadata``. With target_size = 100 the cut
        is comfortably wide enough to capture all 10 sentinels
        regardless of production rank collisions; the assertion is
        "sentinels appear in the new broad tier," not "broad tier is
        exclusively sentinels."

        This was originally written as "top 5 sentinels at target=5"
        but failed when a production item (★ Butterfly Knife | Fade)
        happened to share rank 5 with a sentinel and won the tiebreak.
        The wider cut makes the test robust to production data state.
        """
        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)

        rc = seed_run(
            watchlist_path=path,
            target_size=100,
            dry_run=False,
            file=io.StringIO(),
        )
        assert rc == 0

        from scripts.watchlist_edit import _make_yaml

        with path.open() as f:
            data = _make_yaml().load(f)
        _deep, broad = partition_yaml_items(data)
        sentinel_names = {name for _, name, _ in sentinel_items}
        assert sentinel_names.issubset(broad), (
            f"sentinels missing from broad tier: "
            f"{sentinel_names - broad}"
        )

    @_db_required
    def test_idempotent(
        self, tmp_path: Path, sentinel_items
    ) -> None:
        """Run the seeder twice against the same metadata. Second
        run produces zero changes — YAML byte-for-byte equal after
        the first write completes."""
        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)

        # First run mutates.
        seed_run(
            watchlist_path=path,
            target_size=5,
            dry_run=False,
            file=io.StringIO(),
        )
        after_first = path.read_bytes()

        # Second run with same inputs should produce no changes.
        seed_run(
            watchlist_path=path,
            target_size=5,
            dry_run=False,
            file=io.StringIO(),
        )
        after_second = path.read_bytes()

        assert after_first == after_second, (
            "seed_broad_tier is not idempotent: second run with "
            "identical inputs produced a different YAML"
        )

    @_db_required
    def test_loader_accepts_seeded_yaml(
        self, tmp_path: Path, sentinel_items
    ) -> None:
        """Sanity check: after a real seed run, the seed_watchlist
        loader accepts the output without raising. Catches any
        accidental schema-violation in our writes (missing tier,
        unknown tier value, etc.)."""
        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)
        seed_run(
            watchlist_path=path,
            target_size=5,
            dry_run=False,
            file=io.StringIO(),
        )

        from scripts.seed_watchlist import load_watchlist

        data = load_watchlist(path)
        assert data["schema_version"] == 2
        # Every item must still have a tier field per loader contract.
        for item in data["items"]:
            assert item.get("tier") in {"deep", "broad"}

    @_db_required
    def test_deep_tier_content_unchanged(
        self, tmp_path: Path, sentinel_items
    ) -> None:
        """Sub-decision (A) reinforcement: a seeder run must leave
        every deep-tier item's *content* unchanged. Note: we compare
        parsed content (dict-equality) rather than raw line bytes,
        because ruamel's round-trip reformats flow-style entries
        (e.g. drops inner-brace spaces). That formatting reflow is a
        one-time cost on the first seeder run against any
        previously-hand-edited YAML; it's documented in the seeder's
        docstring. The semantic invariant — 'deep-tier items aren't
        touched by the broad-tier seeder' — still holds.
        """
        from scripts.watchlist_edit import _make_yaml

        path = tmp_path / "wl.yaml"
        path.write_text(_MIN_YAML)

        # Parse the deep-tier item dicts before the run.
        with path.open() as f:
            data_before = _make_yaml().load(f)
        deep_before = sorted(
            (
                dict(item)
                for item in data_before.get("items", [])
                if item.get("tier") == "deep"
            ),
            key=lambda d: d["market_hash_name"],
        )

        seed_run(
            watchlist_path=path,
            target_size=5,
            dry_run=False,
            file=io.StringIO(),
        )

        with path.open() as f:
            data_after = _make_yaml().load(f)
        deep_after = sorted(
            (
                dict(item)
                for item in data_after.get("items", [])
                if item.get("tier") == "deep"
            ),
            key=lambda d: d["market_hash_name"],
        )

        assert deep_before == deep_after, (
            "deep-tier item content changed during a broad-tier seed; "
            "deep tier is editorial and must never be touched by "
            "this seeder"
        )


# Reference the module so a future import-pruner doesn't strip it.
_ = seed_broad_tier
