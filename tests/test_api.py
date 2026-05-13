"""FastAPI read-API tests.

Uses ``fastapi.testclient.TestClient`` against the real app. The DB is
the same Postgres the rest of the suite hits; tests create a sentinel
item (name no real CS2 watchlist would carry), insert known prices,
and clean up after themselves.

Skip pattern matches ``test_db_roundtrip.py``: DATABASE_URL must be
set and reachable. The ``_preserve_source_enabled_flags`` autouse
fixture (same pattern as ``test_watchlist_edit.py``) snapshots and
restores ``sources.enabled`` so a test that toggles a source does not
mutate the operator's flag state.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from api.routes.deals import (
    AT_MARKET_TOLERANCE_PCT,
    COMPARABLE_FRESHNESS_HOURS,
)
from db.connection import get_engine
from db.models import Item, Price, Source


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


_SENTINEL_NAME = "__APITest__ | Sentinel (Field-Tested)"
_SENTINEL_SLUG = "apitest-sentinel-field-tested"

# Phase 6.6: every authenticated test runs with a known token set in
# the environment via the autouse fixture. Authenticated clients carry
# the matching Authorization header; tests that exercise the auth
# itself construct their own clients with no/wrong header.
_TEST_TOKEN = "test-token-deadbeefcafebabe1234567890"


@pytest.fixture(autouse=True)
def _set_api_token(monkeypatch):
    """Ensure ``SKIN_MARKET_API_TOKEN`` is set for every test in this
    file. Auth fails closed when unset, so tests that don't
    monkeypatch.delenv() will see authenticated routes accept the
    matching bearer token."""
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)


@pytest.fixture
def client() -> TestClient:
    """TestClient pre-authenticated with the matching bearer token.

    ``httpx.Client.headers`` is mutable and propagates to all calls
    against the same client, so we don't have to thread headers
    through every test invocation."""
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
    return c


@pytest.fixture(autouse=True)
def _preserve_source_enabled_flags():
    """Snapshot + restore sources.enabled for every test in this file —
    same pattern as test_watchlist_edit.py. Some tests here toggle
    flags to exercise the WHERE enabled=TRUE path."""
    if not _db_reachable():
        yield
        return
    engine = get_engine()
    with Session(engine) as session:
        snapshot = {
            row.name: row.enabled
            for row in session.execute(
                select(Source.name, Source.enabled)
            ).all()
        }
    yield
    with Session(engine) as session:
        for name, was_enabled in snapshot.items():
            session.execute(
                text(
                    "UPDATE sources SET enabled = :e WHERE name = :n"
                ),
                {"e": was_enabled, "n": name},
            )
        session.commit()


@pytest.fixture
def sentinel_item():
    """Insert the sentinel item; yield its UUID; clean up prices +
    insights + item itself afterward."""
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
            select(Item.id).where(Item.market_hash_name == _SENTINEL_NAME)
        ).scalar_one()
        session.execute(
            text("DELETE FROM prices WHERE item_id = :i"),
            {"i": item_id},
        )
        session.execute(
            text("DELETE FROM insights WHERE item_id = :i"),
            {"i": item_id},
        )
        session.commit()

        yield item_id

        session.execute(
            text("DELETE FROM prices WHERE item_id = :i"),
            {"i": item_id},
        )
        session.execute(
            text("DELETE FROM insights WHERE item_id = :i"),
            {"i": item_id},
        )
        session.execute(
            text("DELETE FROM items WHERE id = :i"), {"i": item_id}
        )
        session.commit()


def _source_id(session: Session, name: str) -> int:
    return session.execute(
        select(Source.id).where(Source.name == name)
    ).scalar_one()


def _ensure_source_enabled(session: Session, name: str) -> None:
    session.execute(
        text("UPDATE sources SET enabled = TRUE WHERE name = :n"),
        {"n": name},
    )


def _insert_price(
    session: Session,
    item_id: uuid.UUID,
    source_name: str,
    timestamp: datetime,
    price: str,
    volume: int = 10,
) -> None:
    session.execute(
        pg_insert(Price)
        .values(
            item_id=item_id,
            source_id=_source_id(session, source_name),
            timestamp=timestamp,
            price=Decimal(price),
            volume=volume,
            currency="USD",
            raw_response={"synthetic": True},
        )
        .on_conflict_do_nothing(
            index_elements=["item_id", "source_id", "timestamp"]
        )
    )


# ---------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------


@_db_required
class TestHealth:
    def test_health_reports_db_reachable(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "reachable"


class TestAuth:
    """Single static bearer token (api.auth.require_token) gates every
    router. /health is the explicit exception. ADR 014 §10."""

    def test_missing_authorization_header_401(self) -> None:
        c = TestClient(app)  # no Authorization header
        resp = c.get("/items")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_invalid_token_401(self) -> None:
        c = TestClient(app)
        c.headers["Authorization"] = "Bearer wrong-token-on-purpose"
        resp = c.get("/items")
        assert resp.status_code == 401

    def test_malformed_authorization_header_401(self) -> None:
        c = TestClient(app)
        # Not "Bearer X" — wrong scheme.
        c.headers["Authorization"] = f"Basic {_TEST_TOKEN}"
        resp = c.get("/items")
        assert resp.status_code == 401

    @_db_required
    def test_valid_token_passes(self, client: TestClient) -> None:
        # ``client`` fixture has the matching Bearer header.
        resp = client.get("/items")
        assert resp.status_code == 200

    def test_health_bypasses_auth(self) -> None:
        """``/health`` must respond without any Authorization header —
        Docker healthchecks have no credentials and an operator
        running ``curl http://localhost:8000/health`` against a
        misconfigured auth state must still see the API's actual
        status. ADR 014 §10."""
        c = TestClient(app)  # no Authorization header
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_env_var_unset_returns_500(
        self, monkeypatch
    ) -> None:
        """Defense in depth: if SKIN_MARKET_API_TOKEN is unset at
        request time, auth fails closed with 500 — never silently
        accepts requests."""
        monkeypatch.delenv("SKIN_MARKET_API_TOKEN", raising=False)
        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
        resp = c.get("/items")
        assert resp.status_code == 500
        assert "auth is not configured" in resp.json()["detail"].lower()

    def test_constant_time_comparison_used(self) -> None:
        """Sanity-check the implementation uses secrets.compare_digest
        rather than ``==`` — protects against any future hand-edits
        that could introduce a timing oracle on token comparison.
        Asserts on the imported symbol; not a runtime behavior test."""
        import inspect

        from api import auth

        src = inspect.getsource(auth.require_token)
        assert "compare_digest" in src, (
            "Token comparison must use secrets.compare_digest"
        )


# ---------------------------------------------------------------------
# /items, /items/{slug}
# ---------------------------------------------------------------------


@_db_required
class TestItems:
    def test_list_items_includes_sentinel(
        self, client: TestClient, sentinel_item
    ) -> None:
        resp = client.get("/items")
        assert resp.status_code == 200
        slugs = {row["slug"] for row in resp.json()}
        assert _SENTINEL_SLUG in slugs

    def test_get_item_happy(
        self, client: TestClient, sentinel_item
    ) -> None:
        resp = client.get(f"/items/{_SENTINEL_SLUG}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == _SENTINEL_SLUG
        assert body["market_hash_name"] == _SENTINEL_NAME
        assert body["item_type"] == "rifle"
        assert body["wear"] == "Field-Tested"
        assert body["is_stattrak"] is False
        assert body["is_souvenir"] is False

    def test_get_item_unknown_slug_404(self, client: TestClient) -> None:
        resp = client.get("/items/__definitely-not-a-real-slug__")
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# /items/{slug}/price
# ---------------------------------------------------------------------


@_db_required
class TestPrice:
    def test_multi_source_shape_with_denominations(
        self, client: TestClient, sentinel_item
    ) -> None:
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "steam_market")
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(minutes=10), "28.00", volume=27,
            )
            _insert_price(
                session, sentinel_item, "dmarket",
                now - timedelta(minutes=8), "31.41", volume=12,
            )
            _insert_price(
                session, sentinel_item, "steam_market",
                now - timedelta(minutes=5), "42.92", volume=99,
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        assert resp.status_code == 200
        body = resp.json()

        # No collapsed "price" field at the top level — that would
        # silently combine across denominations.
        assert "price" not in body
        assert body["slug"] == _SENTINEL_SLUG
        assert isinstance(body["sources"], list)
        assert len(body["sources"]) == 3

        by_source = {s["source"]: s for s in body["sources"]}
        # Every row carries a denomination tag (architectural invariant).
        assert by_source["skinport"]["denomination"] == "usd"
        assert by_source["dmarket"]["denomination"] == "usd"
        assert by_source["steam_market"]["denomination"] == "wallet_credit"

        # Money is on the wire as a STRING, not a float.
        assert by_source["skinport"]["price"] == "28.00"
        assert isinstance(by_source["skinport"]["price"], str)
        assert by_source["steam_market"]["price"] == "42.92"

        # observed_at is present on every row.
        for s in body["sources"]:
            assert s["observed_at"] is not None

    def test_picks_latest_per_source(
        self, client: TestClient, sentinel_item
    ) -> None:
        """When a source has multiple observations, /price returns the
        most recent only — DISTINCT ON (source_id) … ORDER BY ts DESC."""
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(hours=2), "20.00",
            )
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(minutes=5), "28.00",
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        skinport = next(
            s for s in resp.json()["sources"] if s["source"] == "skinport"
        )
        assert skinport["price"] == "28.00"  # newer wins

    def test_disabled_source_omitted(
        self, client: TestClient, sentinel_item
    ) -> None:
        """sources.enabled = FALSE drops the row from /price — same
        WHERE clause the scheduler and analytics already respect."""
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "steam_market")
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(minutes=5), "28.00",
            )
            _insert_price(
                session, sentinel_item, "steam_market",
                now - timedelta(minutes=5), "42.92",
            )
            session.execute(
                text(
                    "UPDATE sources SET enabled = FALSE "
                    "WHERE name = 'skinport'"
                )
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        sources = {s["source"] for s in resp.json()["sources"]}
        assert "skinport" not in sources
        assert "steam_market" in sources

    def test_empty_sources_when_no_observations(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Known slug but no prices yet: 200 with empty sources list,
        NOT a 404. Distinguishes 'I don't track that item' (which IS
        a 404) from 'I track it but have no data yet'."""
        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        assert resp.status_code == 200
        assert resp.json()["sources"] == []

    def test_unknown_slug_404(self, client: TestClient) -> None:
        resp = client.get("/items/__definitely-not-real__/price")
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# /items/{slug}/history
# ---------------------------------------------------------------------


@_db_required
class TestHistory:
    def test_default_window_returns_recent_observations(
        self, client: TestClient, sentinel_item
    ) -> None:
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            # 5 rows in the default 7-day window
            for i in range(5):
                _insert_price(
                    session, sentinel_item, "skinport",
                    now - timedelta(days=i),
                    f"{20 + i}.00",
                )
            # One ancient row — older than 7d default
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(days=30), "5.00",
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 5
        assert body["limit"] == 500  # the default
        # Money serialized as string everywhere.
        for obs in body["observations"]:
            assert isinstance(obs["price"], str)

    def test_source_filter(
        self, client: TestClient, sentinel_item
    ) -> None:
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(hours=1), "28.00",
            )
            _insert_price(
                session, sentinel_item, "dmarket",
                now - timedelta(hours=1), "31.41",
            )
            session.commit()

        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/history?source=skinport"
        )
        body = resp.json()
        sources_seen = {o["source"] for o in body["observations"]}
        assert sources_seen == {"skinport"}

    def test_since_after_until_400(
        self, client: TestClient, sentinel_item
    ) -> None:
        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/history"
            "?since=2030-01-02T00:00:00Z&until=2030-01-01T00:00:00Z"
        )
        assert resp.status_code == 400

    def test_limit_cap_enforced(
        self, client: TestClient, sentinel_item
    ) -> None:
        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/history?limit=999999"
        )
        # Pydantic validates le=5000 and returns 422.
        assert resp.status_code == 422

    def test_unknown_slug_404(self, client: TestClient) -> None:
        resp = client.get("/items/__definitely-not-real__/history")
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# /items/{slug}/insights
# ---------------------------------------------------------------------


@_db_required
class TestInsights:
    def test_latest_of_each_type_sub_key(
        self, client: TestClient, sentinel_item
    ) -> None:
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            skinport_id = _source_id(session, "skinport")
            # Two MA insights for skinport — newer must win.
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type, value, meta_info
                    )
                    VALUES
                        (
                            :i, :t1, 'moving_avg_7d', 25.00,
                            CAST(:m1 AS jsonb)
                        ),
                        (
                            :i, :t2, 'moving_avg_7d', 30.00,
                            CAST(:m2 AS jsonb)
                        )
                    """
                ),
                {
                    "i": sentinel_item,
                    "t1": now - timedelta(hours=2),
                    "t2": now - timedelta(minutes=5),
                    "m1": json.dumps(
                        {
                            "source_id": skinport_id,
                            "source_name": "skinport",
                            "n_samples": 10,
                        }
                    ),
                    "m2": json.dumps(
                        {
                            "source_id": skinport_id,
                            "source_name": "skinport",
                            "n_samples": 12,
                        }
                    ),
                },
            )
            # daily_narrative — must be EXCLUDED from per-item endpoint.
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type, value,
                        text_value, meta_info
                    )
                    VALUES (
                        :i, :t, 'daily_narrative', NULL, :txt,
                        CAST(:m AS jsonb)
                    )
                    """
                ),
                {
                    "i": sentinel_item,
                    "t": now - timedelta(minutes=10),
                    "txt": "synthetic narrative for test",
                    "m": json.dumps({}),
                },
            )
            session.commit()
            _ = steam_id  # silence linter on the unused variable

        resp = client.get(f"/items/{_SENTINEL_SLUG}/insights")
        assert resp.status_code == 200
        body = resp.json()
        types = [i["insight_type"] for i in body["insights"]]
        assert "moving_avg_7d" in types
        assert "daily_narrative" not in types
        ma_rows = [
            i
            for i in body["insights"]
            if i["insight_type"] == "moving_avg_7d"
        ]
        # Only the latest skinport row should appear.
        assert len(ma_rows) == 1
        assert ma_rows[0]["value"] == "30.00"

    def test_unknown_slug_404(self, client: TestClient) -> None:
        resp = client.get("/items/__definitely-not-real__/insights")
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# /items/{slug}/chart
# ---------------------------------------------------------------------


@_db_required
class TestChart:
    def test_chart_returns_png_bytes(
        self, client: TestClient, sentinel_item
    ) -> None:
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            for i in range(5):
                _insert_price(
                    session, sentinel_item, "skinport",
                    now - timedelta(hours=i),
                    f"{20 + i}.00",
                )
            session.commit()

        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/chart?source=skinport&days=7"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        # PNG magic bytes
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_chart_renders_empty_window_message(
        self, client: TestClient, sentinel_item
    ) -> None:
        """No observations in window: still returns PNG (with a
        readable 'No observations' message), not 404."""
        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/chart?source=skinport&days=1"
        )
        assert resp.status_code == 200
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_chart_unknown_item_or_source_404(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            "/items/__definitely-not-real__/chart?source=skinport"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# /deals/evaluate
# ---------------------------------------------------------------------


@_db_required
class TestDealsEvaluate:
    def _seed_three_sources_fresh(
        self,
        session: Session,
        item_id: uuid.UUID,
        skinport_price: str = "28.00",
        dmarket_price: str = "31.41",
        steam_price: str = "42.92",
    ) -> None:
        now = datetime.now(UTC)
        _ensure_source_enabled(session, "steam_market")
        _ensure_source_enabled(session, "skinport")
        _ensure_source_enabled(session, "dmarket")
        _insert_price(
            session, item_id, "skinport",
            now - timedelta(minutes=5), skinport_price,
        )
        _insert_price(
            session, item_id, "dmarket",
            now - timedelta(minutes=5), dmarket_price,
        )
        _insert_price(
            session, item_id, "steam_market",
            now - timedelta(minutes=5), steam_price,
        )

    def test_usd_offer_above_market(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        with Session(engine) as session:
            self._seed_three_sources_fresh(session, sentinel_item)
            session.commit()

        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "42.50", "currency": "usd"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "above_market"
        # Comparable = the two USD sources, freshly observed.
        comparable_sources = {c["source"] for c in body["comparable"]}
        assert comparable_sources == {"skinport", "dmarket"}
        # Informational = the wallet-credit source.
        informational_sources = {
            i["source"] for i in body["informational"]
        }
        assert informational_sources == {"steam_market"}
        steam_info = next(
            i for i in body["informational"] if i["source"] == "steam_market"
        )
        assert steam_info["reason"] == "denomination_mismatch"
        # Offer amount round-trips as string.
        assert body["offer"]["amount"] == "42.50"

    def test_usd_offer_below_market(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        with Session(engine) as session:
            self._seed_three_sources_fresh(session, sentinel_item)
            session.commit()

        # Offer at $15 vs cheapest comparable $28 — clearly below.
        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "15.00", "currency": "usd"},
            },
        )
        assert resp.json()["verdict"] == "below_market"

    def test_usd_offer_at_market_within_tolerance(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Offer within ±AT_MARKET_TOLERANCE_PCT of the cheapest
        comparable is at_market. With cheapest=$28 and tolerance=5%,
        the window is $26.60 .. $29.40."""
        engine = get_engine()
        with Session(engine) as session:
            self._seed_three_sources_fresh(session, sentinel_item)
            session.commit()

        cheapest = Decimal("28.00")
        upper = cheapest * (Decimal("1") + AT_MARKET_TOLERANCE_PCT)
        offer = (cheapest + upper) / Decimal("2")  # well inside band
        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": str(offer), "currency": "usd"},
            },
        )
        assert resp.json()["verdict"] == "at_market"

    def test_wallet_credit_offer_compares_only_steam(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        with Session(engine) as session:
            self._seed_three_sources_fresh(session, sentinel_item)
            session.commit()

        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {
                    "amount": "500.00",
                    "currency": "wallet_credit",
                },
            },
        )
        body = resp.json()
        comparable_sources = {c["source"] for c in body["comparable"]}
        assert comparable_sources == {"steam_market"}
        informational_sources = {
            i["source"] for i in body["informational"]
        }
        assert informational_sources == {"skinport", "dmarket"}
        for info in body["informational"]:
            assert info["reason"] == "denomination_mismatch"
        # 500 SC vs 42.92 SC — way above market.
        assert body["verdict"] == "above_market"

    def test_stale_comparable_demoted(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Comparable in matching currency but >4h old is demoted to
        informational with reason=stale."""
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            _insert_price(
                session, sentinel_item, "skinport",
                # Past the freshness floor.
                now - timedelta(hours=COMPARABLE_FRESHNESS_HOURS + 1),
                "28.00",
            )
            _insert_price(
                session, sentinel_item, "dmarket",
                now - timedelta(minutes=5),
                "31.41",
            )
            session.commit()

        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "30.00", "currency": "usd"},
            },
        )
        body = resp.json()
        comparable_sources = {c["source"] for c in body["comparable"]}
        # Only DMarket is fresh; Skinport demoted to informational/stale.
        assert comparable_sources == {"dmarket"}
        stale_rows = [
            i for i in body["informational"] if i["reason"] == "stale"
        ]
        assert len(stale_rows) == 1
        assert stale_rows[0]["source"] == "skinport"

    def test_no_comparable_data_when_all_stale(
        self, client: TestClient, sentinel_item
    ) -> None:
        """All matching-currency sources are stale → verdict=no_comparable_data,
        informational still populated for context."""
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(hours=COMPARABLE_FRESHNESS_HOURS + 1),
                "28.00",
            )
            _insert_price(
                session, sentinel_item, "dmarket",
                now - timedelta(hours=COMPARABLE_FRESHNESS_HOURS + 1),
                "31.41",
            )
            session.commit()

        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "30.00", "currency": "usd"},
            },
        )
        body = resp.json()
        assert body["verdict"] == "no_comparable_data"
        assert body["comparable"] == []
        stale_count = sum(
            1 for i in body["informational"] if i["reason"] == "stale"
        )
        assert stale_count == 2

    def test_unknown_slug_404(self, client: TestClient) -> None:
        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": "__definitely-not-real__",
                "offer": {"amount": "1.00", "currency": "usd"},
            },
        )
        assert resp.status_code == 404

    def test_money_round_trips_as_string(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        with Session(engine) as session:
            self._seed_three_sources_fresh(session, sentinel_item)
            session.commit()

        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "42.50", "currency": "usd"},
            },
        )
        body = resp.json()
        # Every money field is a string, not a float.
        assert isinstance(body["offer"]["amount"], str)
        for c in body["comparable"]:
            assert isinstance(c["current"], str)
            assert isinstance(c["delta"], str)
        for i in body["informational"]:
            # current may be null (no_data case), but if present it's a string.
            if i["current"] is not None:
                assert isinstance(i["current"], str)


@_db_required
class TestOpenAPIDoc:
    def test_openapi_includes_examples_on_substantive_endpoints(
        self, client: TestClient
    ) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        # The components.schemas dict carries examples we declared via
        # model_config json_schema_extra. Just verify the three
        # advertise examples — protects against accidentally dropping
        # the model_config block during a refactor.
        components = schema.get("components", {}).get("schemas", {})
        for model_name in (
            "PriceResponse",
            "HistoryResponse",
            "DealEvaluateRequest",
            "DealEvaluateResponse",
        ):
            assert model_name in components, model_name
            assert "examples" in components[model_name], model_name
