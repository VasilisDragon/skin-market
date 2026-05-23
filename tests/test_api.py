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
            text("DELETE FROM observation_log WHERE item_id = :i"),
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
            text("DELETE FROM observation_log WHERE item_id = :i"),
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


def _upsert_observation_log(
    session: Session,
    item_id: uuid.UUID,
    source_name: str,
    last_observed_at: datetime,
) -> None:
    """Upsert observation_log for the sentinel item. The /items/{slug}/price
    query is driven off observation_log (ADR 017) — tests that hit /price
    must therefore seed observation_log as well as prices.

    The fixture's prices DELETE cascades cleanly via FK ordering; this
    helper additionally clears observation_log on rollback via the
    sentinel cleanup hook below.
    """
    session.execute(
        text(
            """
            INSERT INTO observation_log
                (item_id, source_id, last_observed_at)
            VALUES (:i, :s, :ts)
            ON CONFLICT (item_id, source_id)
            DO UPDATE SET last_observed_at = EXCLUDED.last_observed_at
            """
        ),
        {
            "i": item_id,
            "s": _source_id(session, source_name),
            "ts": last_observed_at,
        },
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

    @_db_required
    def test_health_bypasses_auth(self) -> None:
        """``/health`` must respond without any Authorization header —
        Docker healthchecks have no credentials and an operator
        running ``curl http://localhost:8000/health`` against a
        misconfigured auth state must still see the API's actual
        status. ADR 014 §10.

        Marked ``@_db_required`` because the /health route reports
        DB reachability (see TestHealth), so it issues a SELECT 1
        through the engine; without a reachable Postgres the route
        raises rather than returning 200."""
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

    @_db_required
    def test_multiple_tokens_all_authenticate(
        self, monkeypatch
    ) -> None:
        """SKIN_MARKET_API_TOKENS (plural) is comma-separated; any
        token in the set authenticates. v1 single-consumer uses the
        singular alias; Phase 8+ multi-consumer uses this knob.
        ADR 014 §10.

        Marked ``@_db_required`` because the test asserts a 200 on
        ``/items``, which lists from the DB; without a reachable
        Postgres the route raises before auth's behavior can be
        observed."""
        monkeypatch.delenv("SKIN_MARKET_API_TOKEN", raising=False)
        monkeypatch.setenv(
            "SKIN_MARKET_API_TOKENS",
            "tokenA, tokenB ,tokenC",  # spaces + missing trim cases
        )
        for token in ("tokenA", "tokenB", "tokenC"):
            c = TestClient(app)
            c.headers["Authorization"] = f"Bearer {token}"
            resp = c.get("/items")
            assert resp.status_code == 200, (
                f"token {token!r} should authenticate but didn't"
            )

    def test_unrelated_token_rejected_against_set(
        self, monkeypatch
    ) -> None:
        """A token NOT in the configured set must 401 — set
        membership, not prefix or substring match."""
        monkeypatch.delenv("SKIN_MARKET_API_TOKEN", raising=False)
        monkeypatch.setenv("SKIN_MARKET_API_TOKENS", "tokenA,tokenB")
        c = TestClient(app)
        c.headers["Authorization"] = "Bearer tokenC"
        resp = c.get("/items")
        assert resp.status_code == 401

    def test_empty_token_set_fails_closed(self, monkeypatch) -> None:
        """Both env vars empty/unset → 500 (fail closed). Silently
        treating "no tokens configured" as "auth disabled" would be a
        footgun — surface the misconfiguration."""
        monkeypatch.delenv("SKIN_MARKET_API_TOKEN", raising=False)
        monkeypatch.setenv("SKIN_MARKET_API_TOKENS", "   ,  , ")
        c = TestClient(app)
        c.headers["Authorization"] = "Bearer anything"
        resp = c.get("/items")
        assert resp.status_code == 500
        assert "auth is not configured" in resp.json()["detail"].lower()

    @_db_required
    def test_plural_and_singular_unioned(self, monkeypatch) -> None:
        """If both env vars are set, the accepted set is their union —
        useful when an operator phases in a new client without
        disrupting the existing one.

        Marked ``@_db_required`` for the same reason as
        ``test_multiple_tokens_all_authenticate``: asserts on a
        DB-touching ``/items`` response."""
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", "primary")
        monkeypatch.setenv("SKIN_MARKET_API_TOKENS", "secondary,tertiary")
        for token in ("primary", "secondary", "tertiary"):
            c = TestClient(app)
            c.headers["Authorization"] = f"Bearer {token}"
            assert client_status_ok(c.get("/items"), token)


def client_status_ok(response, token: str) -> bool:
    """Helper for the union test: asserts and returns True so the
    parametric loop reads naturally."""
    assert response.status_code == 200, (
        f"token {token!r} should authenticate but did not: "
        f"{response.status_code} {response.text}"
    )
    return True


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

    def test_list_items_carries_structured_fields(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Phase 2b Step 9: weapon_name, skin_name, is_stattrak,
        is_souvenir surface on the list endpoint so the bot can
        match orphan slugs to their active deep-tier sibling wear
        without parsing display_name."""
        resp = client.get("/items")
        assert resp.status_code == 200
        row = next(
            r for r in resp.json() if r["slug"] == _SENTINEL_SLUG
        )
        assert row["weapon_name"] == "TestWeapon"
        assert row["skin_name"] == "Sentinel"
        assert row["is_stattrak"] is False
        assert row["is_souvenir"] is False
        assert "tier" in row  # Step 8 field still present

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
            # observation_log mirrors the prices timestamps — fresh
            # poll, fresh change. The interesting divergence is tested
            # separately in test_polled_fresh_but_price_flat.
            for source in ("skinport", "dmarket", "steam_market"):
                _upsert_observation_log(
                    session, sentinel_item, source, now,
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

        # Both timestamps present on every row (ADR 017). The
        # `observed_at` field is GONE — clients must read
        # last_polled_at (staleness) and last_changed_at (informational).
        for s in body["sources"]:
            assert s["last_polled_at"] is not None
            assert s["last_changed_at"] is not None
            assert "observed_at" not in s

    def test_picks_latest_per_source(
        self, client: TestClient, sentinel_item
    ) -> None:
        """When a source has multiple observations, /price returns the
        most recent only — LATERAL (SELECT … ORDER BY ts DESC LIMIT 1)."""
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
            _upsert_observation_log(
                session, sentinel_item, "skinport", now,
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
            _upsert_observation_log(
                session, sentinel_item, "skinport", now,
            )
            _upsert_observation_log(
                session, sentinel_item, "steam_market", now,
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

    def test_polled_fresh_but_price_flat(
        self, client: TestClient, sentinel_item
    ) -> None:
        """ADR 017 / Phase 1 fix: the two timestamps diverge when the
        collector polls cleanly but the dedup gate (ADR 009 §3)
        suppresses the write. Skinport in steady state shows this for
        ~45/48 items per cycle.

        Expected: last_polled_at is fresh (minutes ago),
        last_changed_at is multi-hour-old. Confirms the two fields are
        sourced from observation_log and prices separately.
        """
        now = datetime.now(UTC)
        old_price_ts = now - timedelta(hours=16)
        fresh_poll_ts = now - timedelta(minutes=2)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _insert_price(
                session, sentinel_item, "skinport",
                old_price_ts, "28.00",
            )
            _upsert_observation_log(
                session, sentinel_item, "skinport", fresh_poll_ts,
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        assert resp.status_code == 200
        skinport = next(
            s for s in resp.json()["sources"] if s["source"] == "skinport"
        )
        # last_polled_at tracks observation_log
        assert (
            datetime.fromisoformat(
                skinport["last_polled_at"].replace("Z", "+00:00")
            )
            - fresh_poll_ts
        ).total_seconds() < 1.0
        # last_changed_at tracks prices.timestamp
        assert (
            datetime.fromisoformat(
                skinport["last_changed_at"].replace("Z", "+00:00")
            )
            - old_price_ts
        ).total_seconds() < 1.0

    def test_source_without_observation_log_omitted(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Per ADR 017: the /price query drives off observation_log.
        A source with a stale prices row but no observation_log row
        (e.g. DMarket items that fall through the title-mismatch
        guard) is omitted from the response — the bot's never_observed
        branch fills in the slot at render time.
        """
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            # Skinport has both prices AND observation_log (normal).
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(minutes=10), "28.00",
            )
            _upsert_observation_log(
                session, sentinel_item, "skinport", now,
            )
            # DMarket has a stale prices row but no observation_log
            # row — modelling the Moto-Gloves-on-DMarket scenario.
            _insert_price(
                session, sentinel_item, "dmarket",
                now - timedelta(days=3), "31.41",
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/price")
        sources = {s["source"] for s in resp.json()["sources"]}
        assert "skinport" in sources
        assert "dmarket" not in sources, (
            "A source with prices but no observation_log must be "
            "omitted — the bot fills with never_observed."
        )

    def test_schema_rejects_missing_last_polled_at(self) -> None:
        """Pure schema test: ``PerSourcePrice`` must reject a request
        body missing ``last_polled_at`` (the new freshness signal).
        Catches accidental schema drift if the API stops surfacing
        this field.
        """
        from pydantic import ValidationError

        from api.schemas import PerSourcePrice

        with pytest.raises(ValidationError) as exc_info:
            PerSourcePrice(
                source="skinport",
                denomination="usd",
                price=Decimal("28.00"),
                volume=27,
                # last_polled_at intentionally omitted
                last_changed_at=datetime.now(UTC),
            )
        assert "last_polled_at" in str(exc_info.value)

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

    def test_chart_single_observation_in_window(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Phase 8a edge case: a window with exactly one observation
        must still render (not crash on min(prices) over a 1-element
        list or similar). The fill_between under the curve uses
        ``min(prices)`` which is well-defined for length-1 lists,
        but the test pins the contract."""
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _insert_price(
                session, sentinel_item, "skinport",
                now - timedelta(hours=2), "33.06",
            )
            session.commit()

        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/chart?source=skinport&days=7"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
        # Non-trivial body so we know the chart actually rendered
        # (not just an empty PNG stub).
        assert len(resp.content) > 3000

    def test_chart_days_equal_one(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Phase 8a: days=1 takes the HourLocator branch of
        _apply_chart_style. Edge of the date-locator decision tree."""
        resp = client.get(
            f"/items/{_SENTINEL_SLUG}/chart?source=skinport&days=1"
        )
        assert resp.status_code == 200
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_chart_long_display_name_does_not_overflow(
        self, client: TestClient
    ) -> None:
        """Phase 8a: items with very long display names get truncated
        in the title so the canvas doesn't overflow. Use the
        underlying helper directly so we don't have to insert a
        synthetic item via the fixture path."""
        import matplotlib

        from api.routes.charts import _apply_chart_style

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        long_name = "★ StatTrak™ " + ("Karambit " * 10) + "Doppler (FN)"
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot([0, 1], [1, 2])
        ax.set_title(f"{long_name} · skinport · last 7d")
        # Must not raise.
        _apply_chart_style(
            fig, ax,
            source="skinport",
            denomination="usd",
            days=7,
        )
        # Sanity-render to make sure savefig doesn't fail on the
        # styled figure.
        import io as _io

        buf = _io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        assert len(buf.getvalue()) > 1000

    def test_chart_y_axis_currency_formatter_usd(
        self, client: TestClient
    ) -> None:
        """Phase 8a contract: USD denomination produces ``$X.XX``
        y-tick labels; wallet_credit produces ``X.XX SC``. Verified
        via the formatter, not pixel inspection."""
        import matplotlib

        from api.routes.charts import _apply_chart_style

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # USD
        fig, ax = plt.subplots()
        ax.plot([0, 1], [10.5, 42.42])
        _apply_chart_style(
            fig, ax, source="skinport", denomination="usd", days=7
        )
        # Force a draw so the formatter has known tick positions.
        fig.canvas.draw()
        labels_usd = [t.get_text() for t in ax.get_yticklabels()]
        assert any(lbl.startswith("$") for lbl in labels_usd)
        plt.close(fig)

        # Wallet credit
        fig, ax = plt.subplots()
        ax.plot([0, 1], [10.5, 42.42])
        _apply_chart_style(
            fig, ax,
            source="steam_market",
            denomination="wallet_credit",
            days=7,
        )
        fig.canvas.draw()
        labels_sc = [t.get_text() for t in ax.get_yticklabels()]
        assert any(lbl.endswith("SC") for lbl in labels_sc)
        plt.close(fig)


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
        """Seed both `prices` and `observation_log` so that all three
        sources read as fresh under the ADR 017 contract — the deals
        endpoint drives freshness off observation_log."""
        now = datetime.now(UTC)
        _ensure_source_enabled(session, "steam_market")
        _ensure_source_enabled(session, "skinport")
        _ensure_source_enabled(session, "dmarket")
        for source, price in (
            ("skinport", skinport_price),
            ("dmarket", dmarket_price),
            ("steam_market", steam_price),
        ):
            _insert_price(
                session, item_id, source,
                now - timedelta(minutes=5), price,
            )
            _upsert_observation_log(
                session, item_id, source, now - timedelta(minutes=5),
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
        """Comparable in matching currency whose last successful poll is
        >4h old is demoted to informational with reason=stale.

        ADR 017: freshness is driven by observation_log.last_observed_at,
        not by prices.timestamp. To exercise the stale path, the
        observation_log row must itself be old (collector hasn't
        polled the source for that item in over 4h).
        """
        engine = get_engine()
        now = datetime.now(UTC)
        stale_ts = now - timedelta(hours=COMPARABLE_FRESHNESS_HOURS + 1)
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            _insert_price(
                session, sentinel_item, "skinport", stale_ts, "28.00",
            )
            _upsert_observation_log(
                session, sentinel_item, "skinport", stale_ts,
            )
            _insert_price(
                session, sentinel_item, "dmarket",
                now - timedelta(minutes=5),
                "31.41",
            )
            _upsert_observation_log(
                session, sentinel_item, "dmarket",
                now - timedelta(minutes=5),
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
        """All matching-currency sources have stale observation_log →
        verdict=no_comparable_data, informational still populated."""
        engine = get_engine()
        now = datetime.now(UTC)
        stale_ts = now - timedelta(hours=COMPARABLE_FRESHNESS_HOURS + 1)
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _ensure_source_enabled(session, "dmarket")
            for source, price in (("skinport", "28.00"), ("dmarket", "31.41")):
                _insert_price(
                    session, sentinel_item, source, stale_ts, price,
                )
                _upsert_observation_log(
                    session, sentinel_item, source, stale_ts,
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

    def test_polled_fresh_but_price_flat_is_comparable(
        self, client: TestClient, sentinel_item
    ) -> None:
        """The Phase 2a fix in a sentence: an (item, source) pair with
        fresh observation_log but multi-hour-stale prices.timestamp
        must NOT be demoted to reason='stale' — it should still anchor
        the verdict.

        Mirrors ``test_polled_fresh_but_price_flat`` for the
        ``/items/{slug}/price`` endpoint. Phase 1 fixed that path;
        Phase 2a applies the same pattern to ``/deals/evaluate``.

        Live empirical analogue: Desert Eagle Blaze FN at $450
        previously returned ``no_comparable_data`` because Skinport's
        prices row was 7+ hours old despite Skinport being polled
        every 15 minutes. After this fix it returns a real verdict.
        """
        engine = get_engine()
        now = datetime.now(UTC)
        old_price_ts = now - timedelta(hours=16)
        fresh_poll_ts = now - timedelta(minutes=2)
        with Session(engine) as session:
            _ensure_source_enabled(session, "skinport")
            _insert_price(
                session, sentinel_item, "skinport", old_price_ts, "754.90",
            )
            _upsert_observation_log(
                session, sentinel_item, "skinport", fresh_poll_ts,
            )
            session.commit()

        resp = client.post(
            "/deals/evaluate",
            json={
                "slug": _SENTINEL_SLUG,
                "offer": {"amount": "450.00", "currency": "usd"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()

        # Skinport must be comparable, not demoted to stale.
        comparable_sources = {c["source"] for c in body["comparable"]}
        assert comparable_sources == {"skinport"}, (
            f"Skinport must be comparable when observation_log is fresh "
            f"even if prices.timestamp is multi-hour stale. "
            f"Got comparable={comparable_sources}, "
            f"informational="
            f"{[(i['source'], i['reason']) for i in body['informational']]}"
        )
        # And the verdict must be a real one (not no_comparable_data).
        assert body["verdict"] == "below_market"
        # last_polled_at carries the fresh observation_log timestamp;
        # last_changed_at carries the old prices.timestamp (informational).
        sk = body["comparable"][0]
        assert (
            datetime.fromisoformat(
                sk["last_polled_at"].replace("Z", "+00:00")
            )
            - fresh_poll_ts
        ).total_seconds() < 1.0
        assert (
            datetime.fromisoformat(
                sk["last_changed_at"].replace("Z", "+00:00")
            )
            - old_price_ts
        ).total_seconds() < 1.0

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
class TestLatestNarrative:
    """``GET /insights/narrative/latest`` — global daily narrative."""

    def test_404_when_no_narrative(self, client: TestClient) -> None:
        # Wipe any existing daily_narrative rows so this test starts
        # from a known-empty state, then restore after.
        engine = get_engine()
        with Session(engine) as session:
            snapshot = (
                session.execute(
                    text(
                        "SELECT computed_at, item_id, text_value, "
                        "meta_info FROM insights "
                        "WHERE insight_type = 'daily_narrative'"
                    )
                )
                .mappings()
                .all()
            )
            session.execute(
                text(
                    "DELETE FROM insights "
                    "WHERE insight_type = 'daily_narrative'"
                )
            )
            session.commit()
        try:
            resp = client.get("/insights/narrative/latest")
            assert resp.status_code == 404
        finally:
            with Session(engine) as session:
                for row in snapshot:
                    session.execute(
                        text(
                            """
                            INSERT INTO insights (
                                item_id, computed_at, insight_type,
                                text_value, meta_info
                            )
                            VALUES (
                                :i, :t, 'daily_narrative', :txt,
                                CAST(:m AS jsonb)
                            )
                            """
                        ),
                        {
                            "i": row["item_id"],
                            "t": row["computed_at"],
                            "txt": row["text_value"],
                            "m": json.dumps(dict(row["meta_info"] or {})),
                        },
                    )
                session.commit()

    def test_returns_latest_when_present(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type,
                        text_value, meta_info
                    )
                    VALUES (
                        :i, :t, 'daily_narrative',
                        'Synthetic test narrative.',
                        CAST(:m AS jsonb)
                    )
                    """
                ),
                {
                    "i": sentinel_item,
                    "t": now,
                    "m": json.dumps({"as_of": now.isoformat()}),
                },
            )
            session.commit()

        try:
            resp = client.get("/insights/narrative/latest")
            assert resp.status_code == 200
            body = resp.json()
            assert body["text"] == "Synthetic test narrative."
            assert "as_of" in body["meta"]
        finally:
            with Session(engine) as session:
                session.execute(
                    text(
                        "DELETE FROM insights "
                        "WHERE insight_type = 'daily_narrative' "
                        "AND computed_at = :t"
                    ),
                    {"t": now},
                )
                session.commit()

    def test_auth_required(self) -> None:
        c = TestClient(app)  # no Authorization header
        resp = c.get("/insights/narrative/latest")
        assert resp.status_code == 401


@_db_required
class TestRecentAnomalies:
    """``GET /insights/anomalies/recent`` — divergence + volume rows
    from the last N hours, joined with item display_name."""

    def test_returns_recent_anomalies(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type, value, meta_info
                    )
                    VALUES
                        (
                            :i, :t1, 'cross_source_divergence', -2.8,
                            CAST(:m1 AS jsonb)
                        ),
                        (
                            :i, :t2, 'volume_anomaly', 2.3,
                            CAST(:m2 AS jsonb)
                        )
                    """
                ),
                {
                    "i": sentinel_item,
                    "t1": now - timedelta(hours=1),
                    "t2": now - timedelta(minutes=30),
                    "m1": json.dumps(
                        {"source_a_id": "1", "source_b_id": "27"}
                    ),
                    "m2": json.dumps({"source_id": 1, "observed_volume": 5}),
                },
            )
            session.commit()

        try:
            resp = client.get("/insights/anomalies/recent?hours=6")
            assert resp.status_code == 200
            body = resp.json()
            sentinel_rows = [
                a for a in body["anomalies"] if a["slug"] == _SENTINEL_SLUG
            ]
            assert len(sentinel_rows) == 2
            kinds = {a["insight_type"] for a in sentinel_rows}
            assert kinds == {"cross_source_divergence", "volume_anomaly"}
            # display_name joined in — bot doesn't need a second lookup.
            for a in sentinel_rows:
                assert a["display_name"] == _SENTINEL_NAME
                assert isinstance(a["z_score"], str)  # money-as-string
        finally:
            with Session(engine) as session:
                session.execute(
                    text(
                        "DELETE FROM insights "
                        "WHERE item_id = :i AND insight_type IN "
                        "('cross_source_divergence', 'volume_anomaly')"
                    ),
                    {"i": sentinel_item},
                )
                session.commit()

    def test_since_filter_excludes_old(
        self, client: TestClient, sentinel_item
    ) -> None:
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type, value, meta_info
                    )
                    VALUES (
                        :i, :t, 'volume_anomaly', 3.1, CAST(:m AS jsonb)
                    )
                    """
                ),
                {
                    "i": sentinel_item,
                    # 12 hours ago — outside the default 6h window.
                    "t": now - timedelta(hours=12),
                    "m": json.dumps({"source_id": 1}),
                },
            )
            session.commit()

        try:
            resp = client.get("/insights/anomalies/recent")
            sentinel_rows = [
                a
                for a in resp.json()["anomalies"]
                if a["slug"] == _SENTINEL_SLUG
            ]
            assert sentinel_rows == []

            resp = client.get("/insights/anomalies/recent?hours=24")
            sentinel_rows = [
                a
                for a in resp.json()["anomalies"]
                if a["slug"] == _SENTINEL_SLUG
            ]
            assert len(sentinel_rows) == 1
        finally:
            with Session(engine) as session:
                session.execute(
                    text(
                        "DELETE FROM insights "
                        "WHERE item_id = :i AND insight_type = 'volume_anomaly'"
                    ),
                    {"i": sentinel_item},
                )
                session.commit()

    def test_hours_param_validation(self, client: TestClient) -> None:
        # hours=0 → 422 (ge=1)
        resp = client.get("/insights/anomalies/recent?hours=0")
        assert resp.status_code == 422
        # hours=99 → 422 (le=24)
        resp = client.get("/insights/anomalies/recent?hours=99")
        assert resp.status_code == 422

    def test_auth_required(self) -> None:
        c = TestClient(app)
        resp = c.get("/insights/anomalies/recent")
        assert resp.status_code == 401


@_db_required
class TestSignalDigest:
    def test_returns_ranked_compact_signals(
        self,
        client: TestClient,
        sentinel_item,
    ) -> None:
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type, value, meta_info
                    )
                    VALUES
                        (
                            :i, :t1, 'cross_source_divergence', -99.0,
                            CAST(:m1 AS jsonb)
                        ),
                        (
                            :i, :t2, 'volume_anomaly', 2.4,
                            CAST(:m2 AS jsonb)
                        )
                    """
                ),
                {
                    "i": sentinel_item,
                    "t1": now - timedelta(minutes=20),
                    "t2": now - timedelta(minutes=10),
                    "m1": json.dumps(
                        {
                            "source_a_id": "1",
                            "source_b_id": "27",
                            "observed_spread": "0.42",
                            "baseline_mean": "0.10",
                        }
                    ),
                    "m2": json.dumps(
                        {
                            "source_id": 1,
                            "observed_volume": 10,
                            "baseline_mean": 3,
                        }
                    ),
                },
            )
            session.commit()

        try:
            resp = client.get("/insights/signals/digest?hours=6&limit=1")
            assert resp.status_code == 200
            body = resp.json()
            assert body["lane"] == "all"
            assert body["total_anomalies"] >= 2
            assert body["returned_count"] == 1
            signal = body["signals"][0]
            assert signal["slug"] == _SENTINEL_SLUG
            assert signal["signal_type"] == "cross_source_divergence"
            assert signal["severity"] == "extreme"
            assert "Spread between 1 and 27 is unusual" in signal["summary"]
        finally:
            with Session(engine) as session:
                session.execute(
                    text(
                        "DELETE FROM insights "
                        "WHERE item_id = :i AND insight_type IN "
                        "('cross_source_divergence', 'volume_anomaly')"
                    ),
                    {"i": sentinel_item},
                )
                session.commit()

    def test_signal_digest_lane_filters_insight_types(
        self,
        client: TestClient,
        sentinel_item,
    ) -> None:
        engine = get_engine()
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    INSERT INTO insights (
                        item_id, computed_at, insight_type, value, meta_info
                    )
                    VALUES
                        (
                            :i, :t1, 'cross_source_divergence', 99.0,
                            CAST(:m1 AS jsonb)
                        ),
                        (
                            :i, :t2, 'volume_anomaly', 98.0,
                            CAST(:m2 AS jsonb)
                        )
                    """
                ),
                {
                    "i": sentinel_item,
                    "t1": now - timedelta(minutes=20),
                    "t2": now - timedelta(minutes=10),
                    "m1": json.dumps(
                        {
                            "source_a_id": "1",
                            "source_b_id": "27",
                            "observed_spread": "0.42",
                            "baseline_mean": "0.10",
                        }
                    ),
                    "m2": json.dumps(
                        {
                            "source_id": 1,
                            "observed_volume": 10,
                            "baseline_mean": 3,
                        }
                    ),
                },
            )
            session.commit()

        try:
            movers = client.get(
                "/insights/signals/digest?lane=market_movers&hours=6&limit=3"
            )
            spreads = client.get(
                "/insights/signals/digest?lane=spread_watch&hours=6&limit=3"
            )
            assert movers.status_code == 200
            assert spreads.status_code == 200
            assert movers.json()["lane"] == "market_movers"
            assert spreads.json()["lane"] == "spread_watch"
            assert movers.json()["signals"][0]["signal_type"] == "volume_anomaly"
            assert (
                spreads.json()["signals"][0]["signal_type"]
                == "cross_source_divergence"
            )
        finally:
            with Session(engine) as session:
                session.execute(
                    text(
                        "DELETE FROM insights "
                        "WHERE item_id = :i AND insight_type IN "
                        "('cross_source_divergence', 'volume_anomaly')"
                    ),
                    {"i": sentinel_item},
                )
                session.commit()

    def test_param_validation(self, client: TestClient) -> None:
        assert client.get("/insights/signals/digest?limit=0").status_code == 422
        assert client.get("/insights/signals/digest?hours=99").status_code == 422
        assert client.get("/insights/signals/digest?lane=unknown").status_code == 422


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
