"""Tests for scripts/seed_catalog.py."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from db.naming import slugify
from scripts import seed_catalog
from scripts.seed_catalog import (
    CatalogMeta,
    ExistingItem,
    SeedCandidate,
    build_candidates,
    build_seed_plan,
    detect_slug_collisions,
    insert_candidates_in_transaction,
    parse_metas,
)


def _watchlist(path: Path, exclusions: list[str] | None = None) -> Path:
    if exclusions:
        exclusions_block = "featured_tier_exclusions:\n" + "\n".join(
            f'  - "{name}"' for name in exclusions
        )
    else:
        exclusions_block = "featured_tier_exclusions: []"
    path.write_text(
        f"""\
schema_version: 3

{exclusions_block}

sources: []
items: []
""",
        encoding="utf-8",
    )
    return path


def _candidate(name: str, rank: int = 1) -> SeedCandidate:
    is_stattrak, is_souvenir = seed_catalog.detect_flags(name)
    return SeedCandidate(
        market_hash_name=name,
        rank=rank,
        slug=slugify(name),
        is_stattrak=is_stattrak,
        is_souvenir=is_souvenir,
    )


class TestParseMetas:
    def test_parses_ranked_names_and_skips_unranked_rows(self) -> None:
        rows = parse_metas(
            [
                {"market_hash_name": "AK-47 | Redline (Field-Tested)", "rank": "2"},
                {"market_hash_name": "No Rank Skin", "rank": None},
                {"market_hash_name": "", "rank": 3},
                {"market_hash_name": "Glock-18 | Fade (Factory New)", "rank": 1.0},
            ]
        )

        assert rows == [
            CatalogMeta("AK-47 | Redline (Field-Tested)", 2),
            CatalogMeta("Glock-18 | Fade (Factory New)", 1),
        ]

    def test_streams_top_level_json_array_content(self) -> None:
        content = b"""[
          {"market_hash_name": "A (Factory New)", "rank": 2},
          {"market_hash_name": "B (Minimal Wear)", "rank": 1}
        ]"""

        rows = parse_metas(seed_catalog._stream_metas_from_content(content))

        assert rows == [
            CatalogMeta("A (Factory New)", 2),
            CatalogMeta("B (Minimal Wear)", 1),
        ]


class TestBuildCandidates:
    def test_selects_top_ranked_items_after_exclusions(self) -> None:
        metas = [
            CatalogMeta("Excluded | Skin (Factory New)", 1),
            CatalogMeta("AK-47 | Redline (Field-Tested)", 2),
            CatalogMeta("Glock-18 | Fade (Factory New)", 3),
        ]

        candidates, duplicate_names_skipped = build_candidates(
            metas,
            exclusions={"Excluded | Skin (Factory New)"},
            limit=2,
        )

        assert duplicate_names_skipped == 0
        assert [c.market_hash_name for c in candidates] == [
            "AK-47 | Redline (Field-Tested)",
            "Glock-18 | Fade (Factory New)",
        ]

    def test_derives_flags_and_slugs_like_watchlist_seed(self) -> None:
        metas = [
            CatalogMeta("StatTrak™ AK-47 | Redline (Field-Tested)", 1),
            CatalogMeta("Souvenir AWP | Dragon Lore (Factory New)", 2),
        ]

        candidates, _ = build_candidates(metas, exclusions=set(), limit=2)

        assert candidates[0].slug == "stattrak-ak-47-redline-field-tested"
        assert candidates[0].is_stattrak is True
        assert candidates[0].is_souvenir is False
        assert candidates[1].slug == "souvenir-awp-dragon-lore-factory-new"
        assert candidates[1].is_stattrak is False
        assert candidates[1].is_souvenir is True

    def test_duplicate_names_do_not_consume_output_slots(self) -> None:
        metas = [
            CatalogMeta("AK-47 | Redline (Field-Tested)", 1),
            CatalogMeta("AK-47 | Redline (Field-Tested)", 2),
            CatalogMeta("Glock-18 | Fade (Factory New)", 3),
        ]

        candidates, duplicate_names_skipped = build_candidates(
            metas,
            exclusions=set(),
            limit=2,
        )

        assert duplicate_names_skipped == 1
        assert [c.market_hash_name for c in candidates] == [
            "AK-47 | Redline (Field-Tested)",
            "Glock-18 | Fade (Factory New)",
        ]


class TestSlugCollisionDetection:
    def test_detects_candidate_candidate_collisions(self) -> None:
        collisions = detect_slug_collisions(
            candidates=[
                _candidate("AK-47 | Redline (Factory New)"),
                _candidate("AK-47 Redline Factory New"),
            ],
            existing_items=[],
        )

        assert collisions == [
            seed_catalog.SlugCollision(
                slug="ak-47-redline-factory-new",
                market_hash_names=(
                    "AK-47 Redline Factory New",
                    "AK-47 | Redline (Factory New)",
                ),
            )
        ]

    def test_detects_candidate_existing_collisions_but_ignores_same_name(
        self,
    ) -> None:
        existing = [
            ExistingItem(
                market_hash_name="AK-47 Redline Factory New",
                slug="ak-47-redline-factory-new",
            ),
            ExistingItem(
                market_hash_name="Glock-18 | Fade (Factory New)",
                slug="glock-18-fade-factory-new",
            ),
        ]

        collisions = detect_slug_collisions(
            candidates=[
                _candidate("AK-47 | Redline (Factory New)"),
                _candidate("Glock-18 | Fade (Factory New)"),
            ],
            existing_items=existing,
        )

        assert len(collisions) == 1
        assert collisions[0].slug == "ak-47-redline-factory-new"


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.execute_calls: list[dict[str, Any]] = []
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, _stmt: object, params: dict[str, Any]) -> _Result:
        if self.fail_at == len(self.execute_calls) + 1:
            raise RuntimeError("boom")
        self.execute_calls.append(params)
        return _Result(rowcount=1)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class TestInsertTransaction:
    def test_inserts_all_candidates_then_commits_once(self) -> None:
        session = _FakeSession()
        candidates = [
            _candidate("StatTrak™ AK-47 | Redline (Field-Tested)", 1),
            _candidate("Glock-18 | Fade (Factory New)", 2),
        ]

        inserted = insert_candidates_in_transaction(session, candidates)  # type: ignore[arg-type]

        assert inserted == 2
        assert session.commit_count == 1
        assert session.rollback_count == 0
        assert session.execute_calls[0]["disp"] == candidates[0].market_hash_name
        assert session.execute_calls[0]["it"] is None
        assert session.execute_calls[0]["wpn"] is None
        assert session.execute_calls[0]["skn"] is None
        assert session.execute_calls[0]["wear"] is None
        assert session.execute_calls[0]["stt"] is True
        assert session.execute_calls[0]["sv"] is False

    def test_rolls_back_without_commit_when_any_insert_fails(self) -> None:
        session = _FakeSession(fail_at=2)
        candidates = [
            _candidate("AK-47 | Redline (Field-Tested)", 1),
            _candidate("Glock-18 | Fade (Factory New)", 2),
        ]

        with pytest.raises(RuntimeError, match="boom"):
            insert_candidates_in_transaction(session, candidates)  # type: ignore[arg-type]

        assert session.commit_count == 0
        assert session.rollback_count == 1


class _NoopSession:
    def __init__(self, _engine: object) -> None:
        pass

    def __enter__(self) -> _NoopSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class TestRun:
    def test_dry_run_fail_fast_on_slug_collision(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        watchlist = _watchlist(tmp_path / "watchlist.yaml")
        monkeypatch.setattr(seed_catalog, "Session", _NoopSession)
        monkeypatch.setattr(
            seed_catalog,
            "load_existing_items",
            lambda _session: [
                ExistingItem(
                    market_hash_name="AK-47 Redline Factory New",
                    slug="ak-47-redline-factory-new",
                )
            ],
        )
        monkeypatch.setattr(
            seed_catalog,
            "insert_candidates_in_transaction",
            lambda _session, _candidates: pytest.fail("must not write"),
        )

        output = io.StringIO()
        rc = seed_catalog.run(
            watchlist_path=watchlist,
            limit=1,
            dry_run=True,
            engine=object(),  # type: ignore[arg-type]
            metas=[CatalogMeta("AK-47 | Redline (Factory New)", 1)],
            file=output,
        )

        assert rc == 2
        assert "Slug collisions: 1" in output.getvalue()
        assert "database unchanged" not in output.getvalue()

    def test_build_seed_plan_counts_existing_and_new_candidates(self) -> None:
        plan = build_seed_plan(
            metas=[
                CatalogMeta("AK-47 | Redline (Field-Tested)", 1),
                CatalogMeta("Glock-18 | Fade (Factory New)", 2),
            ],
            exclusions=set(),
            existing_items=[
                ExistingItem(
                    market_hash_name="AK-47 | Redline (Field-Tested)",
                    slug="ak-47-redline-field-tested",
                )
            ],
            limit=2,
        )

        assert plan.existing_candidates == 1
        assert plan.new_candidates == 1
        assert plan.existing_total == 1
