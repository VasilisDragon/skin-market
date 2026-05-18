"""Tier audit (Phase 2b Step 8): pin broad-tier degradation across
existing endpoints so a future broad-tier population doesn't surprise
the bot.

Broad tier is empty in production today (Step 7.1 set every item's
tier to ``deep`` and broad-tier population is a deferred phase). These
tests classify a synthetic sentinel item as ``broad`` via the
``watchlist_tiers.reload()`` hook and assert each endpoint returns
the documented degraded shape — 200 with a tier-shaped empty
response, NOT a 500 or an indistinguishable-from-deep-with-no-data
result.

For deep-tier behavior, the existing test_api.py + test_api_drift.py
suites already pin the happy path.
"""

from __future__ import annotations

import os
import textwrap
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api import watchlist_tiers
from api.main import app
from db.connection import get_engine


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except (OperationalError, Exception):
        return False


_db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)

_SENTINEL_NAME = "__APITierAuditTest__ | Sentinel (Field-Tested)"
_SENTINEL_SLUG = "apitierauditttest-sentinel-field-tested"
_TEST_TOKEN = "test-token-tier-audit-deadbeefcafe"


@pytest.fixture(autouse=True)
def _set_api_token(monkeypatch):
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)


@pytest.fixture
def client() -> TestClient:
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
    return c


@pytest.fixture(autouse=True)
def _reset_tier_cache():
    watchlist_tiers.reload(watchlist_tiers.DEFAULT_WATCHLIST_PATH)
    yield
    watchlist_tiers.reload(watchlist_tiers.DEFAULT_WATCHLIST_PATH)


@pytest.fixture
def sentinel_item():
    engine = get_engine()
    item_id = uuid.uuid4()
    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO items (
                    id, market_hash_name, display_name, slug, item_type,
                    weapon_name, skin_name, wear
                )
                VALUES (
                    :id, :name, :name, :slug, 'rifle',
                    'TestWeapon', 'Sentinel', 'Field-Tested'
                )
                ON CONFLICT (market_hash_name) DO NOTHING
                """
            ),
            {
                "id": item_id,
                "name": _SENTINEL_NAME,
                "slug": _SENTINEL_SLUG,
            },
        )
        item_id = session.execute(
            text(
                "SELECT id FROM items WHERE market_hash_name = :n"
            ),
            {"n": _SENTINEL_NAME},
        ).scalar_one()
        session.commit()

        yield item_id

        # Clean up any rows the test inserted.
        for table in (
            "insights",
            "prices",
            "observation_log",
        ):
            session.execute(
                text(f"DELETE FROM {table} WHERE item_id = :i"),
                {"i": item_id},
            )
        session.execute(
            text("DELETE FROM items WHERE id = :i"), {"i": item_id}
        )
        session.commit()


def _write_yaml_with_sentinel_tier(path: Path, tier: str) -> None:
    body = f"""\
    schema_version: 3
    sources:
      - name: skinport
        base_url: https://api.skinport.com
        rate_limit_per_minute: 8
        enabled: true
        denomination: usd
    items:
      - market_hash_name: "{_SENTINEL_NAME}"
        item_type: rifle
        weapon_name: "TestWeapon"
        skin_name: "Sentinel"
        wear: "Field-Tested"
        tier: {tier}
    """
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture
def featured_tier_sentinel(sentinel_item, tmp_path: Path):
    """Sentinel classified as broad via tmp YAML + reload."""
    yaml_path = tmp_path / "watchlist.yaml"
    _write_yaml_with_sentinel_tier(yaml_path, "featured")
    watchlist_tiers.reload(yaml_path)
    return sentinel_item


# ---------------------------------------------------------------------
# /items + /items/{slug}
# ---------------------------------------------------------------------


@_db_required
class TestItemsTierField:
    def test_list_items_includes_tier(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        resp = client.get("/items")
        assert resp.status_code == 200
        rows = resp.json()
        sentinel_row = next(
            r for r in rows if r["slug"] == _SENTINEL_SLUG
        )
        assert sentinel_row["tier"] == "featured"

    def test_get_item_includes_tier(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        resp = client.get(f"/items/{_SENTINEL_SLUG}")
        assert resp.status_code == 200
        assert resp.json()["tier"] == "featured"


# ---------------------------------------------------------------------
# /items/{slug}/price — broad → 200 + tier + empty sources
# ---------------------------------------------------------------------


@_db_required
class TestBroadTierPrice:
    def test_featured_tier_price_returns_empty_sources(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        """Pin: broad-tier items get 200 with empty sources[] and
        tier=broad. The bot's Step 9 rendering distinguishes "broad
        tier — curated data not collected for this tier" from
        "unknown item" (404) and from "deep tier but no data yet"
        (200, deep, empty)."""
        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "featured"
        assert body["sources"] == []


# ---------------------------------------------------------------------
# /items/{slug}/history — broad → 200 + tier + empty observations
# ---------------------------------------------------------------------


@_db_required
class TestBroadTierHistory:
    def test_featured_tier_history_returns_empty(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        resp = client.get(f"/items/{_SENTINEL_SLUG}/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "featured"
        assert body["count"] == 0
        assert body["observations"] == []

    def test_unknown_slug_still_404_regardless_of_tier(
        self, client: TestClient
    ) -> None:
        """The tier audit must not break the existing 404 behavior
        for typo'd slugs — the existence check still runs first."""
        resp = client.get("/items/does-not-exist/history")
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# /items/{slug}/insights — broad → 200 + tier + empty insights
# ---------------------------------------------------------------------


@_db_required
class TestBroadTierInsights:
    def test_featured_tier_insights_returns_empty(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        resp = client.get(f"/items/{_SENTINEL_SLUG}/insights")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "featured"
        assert body["insights"] == []


# ---------------------------------------------------------------------
# /items/{slug}/chart — broad → 200 PNG with no-observations frame
# ---------------------------------------------------------------------


@_db_required
class TestBroadTierChart:
    def test_featured_tier_chart_returns_png(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        """The chart endpoint already returns a "No observations in
        the last N days" placeholder PNG when the time-series is
        empty (api/routes/charts.py). Confirm broad-tier items ride
        that path rather than 404'ing."""
        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/chart?source=skinport&days=7"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert len(resp.content) > 0
        # PNG signature: 89 50 4E 47 0D 0A 1A 0A
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------
# POST /deals/evaluate — broad → 200 + tier + no_comparable_data
# ---------------------------------------------------------------------


@_db_required
class TestBroadTierDeals:
    def test_featured_tier_deals_returns_no_comparable_data(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "42.50", "currency": "usd"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "featured"
        assert body["verdict"] == "no_comparable_data"
        assert body["comparable"] == []


# ---------------------------------------------------------------------
# /items/{slug}/drift — broad → 200 + tier=broad + empty pairs
# (also covered in test_api_drift.py; included here as part of the
# audit's "every item endpoint behaves consistently" surface)
# ---------------------------------------------------------------------


@_db_required
class TestBroadTierDriftAudit:
    def test_featured_tier_drift_empty(
        self, client: TestClient, featured_tier_sentinel
    ) -> None:
        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "featured"
        assert body["pairs"] == []
