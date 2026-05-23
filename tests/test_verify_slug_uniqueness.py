"""Tests for the slug uniqueness verifier."""

from __future__ import annotations

from scripts.seed_catalog import CatalogMeta
from scripts.verify_slug_uniqueness import (
    build_name_set,
    detect_slug_collisions,
    run,
    select_ranked_catalog_names,
)


def test_select_ranked_catalog_names_dedupes_and_honors_limit() -> None:
    metas = [
        CatalogMeta("B Item (Factory New)", 2),
        CatalogMeta("A Item (Factory New)", 1),
        CatalogMeta("A Item (Factory New)", 1),
        CatalogMeta("C Item (Factory New)", 3),
    ]

    assert select_ranked_catalog_names(metas, limit=2) == [
        "A Item (Factory New)",
        "B Item (Factory New)",
    ]


def test_build_name_set_dedupes_db_and_catalog_names() -> None:
    assert build_name_set(
        db_names=["AK-47 | Redline (Field-Tested)"],
        catalog_names=[
            "AK-47 | Redline (Field-Tested)",
            "AWP | Asiimov (Field-Tested)",
        ],
    ) == [
        "AK-47 | Redline (Field-Tested)",
        "AWP | Asiimov (Field-Tested)",
    ]


def test_detect_slug_collisions_detects_distinct_names() -> None:
    collisions = detect_slug_collisions(
        [
            "Test Item (Factory New)",
            "Test-Item (Factory New)",
        ]
    )

    assert len(collisions) == 1
    assert collisions[0].slug == "test-item-factory-new"


def test_detect_slug_collisions_accepts_slug_v2_sunset_pair() -> None:
    collisions = detect_slug_collisions(
        [
            "Desert Eagle | Sunset Storm 壱 (Factory New)",
            "Desert Eagle | Sunset Storm 弐 (Factory New)",
        ]
    )

    assert collisions == []


def test_run_returns_nonzero_on_collision(capsys) -> None:
    rc = run(
        catalog_limit=2,
        metas=[
            CatalogMeta("Test Item (Factory New)", 1),
            CatalogMeta("Test-Item (Factory New)", 2),
        ],
        db_names=[],
    )

    assert rc == 1
    assert "Slug collisions: 1" in capsys.readouterr().out
