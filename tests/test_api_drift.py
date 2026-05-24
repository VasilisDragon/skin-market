"""Tests for ``GET /items/{slug}/drift``.

Verdict rows are inserted directly into the insights table to focus
on the route's read-side; the detector's write-side is exercised by
test_drift.py. The fixture mirrors test_api.py's sentinel-item +
autouse-token pattern so the endpoint sees a deterministic items row.
"""

from __future__ import annotations

import json
import os
import textwrap
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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

_SENTINEL_NAME = "__APIDriftTest__ | Sentinel (Factory New)"
_SENTINEL_SLUG = "apidrifttest-sentinel-factory-new"
_TEST_TOKEN = "test-token-drift-deadbeefcafebabe"


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
    """Reset watchlist_tiers cache before AND after each test so tier
    rebindings inside a test never leak. Pointed at the default YAML
    by default; individual tests call reload() with a tmp path."""
    watchlist_tiers.reload(watchlist_tiers.DEFAULT_WATCHLIST_PATH)
    yield
    watchlist_tiers.reload(watchlist_tiers.DEFAULT_WATCHLIST_PATH)


@pytest.fixture
def sentinel_item():
    """Insert the sentinel item, yield its UUID, clean up after."""
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
                    'TestWeapon', 'Sentinel', 'Factory New'
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
        session.execute(
            text("DELETE FROM insights WHERE item_id = :i"),
            {"i": item_id},
        )
        session.commit()

        yield item_id

        session.execute(
            text("DELETE FROM insights WHERE item_id = :i"),
            {"i": item_id},
        )
        session.execute(
            text("DELETE FROM items WHERE id = :i"), {"i": item_id}
        )
        session.commit()


def _insert_drift_verdict(
    session: Session,
    *,
    item_id: uuid.UUID,
    computed_at: datetime,
    verdict: str,
    drift: Decimal | None,
    source_a_id: int,
    source_a_name: str,
    source_b_id: int,
    source_b_name: str,
    classification: str = "pattern_agnostic",
    threshold_multiplier: float = 1.0,
    threshold_used: str = "0.10",
    curated_price: str | None = "100.00",
    pricempire_price: str | None = "98.00",
    curated_age_min: float | None = 2.0,
    pricempire_age_min: float | None = 1.0,
    note: str | None = None,
) -> None:
    """Write a drift_verdict insights row matching the shape
    analytics/drift.py::_build_meta_info produces."""
    meta = {
        "source_a_id": int(source_a_id),
        "source_a_name": source_a_name,
        "source_b_id": int(source_b_id),
        "source_b_name": source_b_name,
        "verdict": verdict,
        "classification": classification,
        "threshold_used": threshold_used,
        "threshold_multiplier": threshold_multiplier,
        "curated_price": curated_price,
        "pricempire_price": pricempire_price,
        "curated_last_polled_at": (
            computed_at - timedelta(minutes=2)
        ).isoformat(),
        "pricempire_last_polled_at": (
            computed_at - timedelta(minutes=1)
        ).isoformat(),
        "curated_age_min": curated_age_min,
        "pricempire_age_min": pricempire_age_min,
        "note": note,
    }
    session.execute(
        text(
            """
            INSERT INTO insights
                (item_id, computed_at, insight_type, value, meta_info)
            VALUES (
                :item_id, :now, 'drift_verdict', :value,
                CAST(:meta AS jsonb)
            )
            """
        ),
        {
            "item_id": item_id,
            "now": computed_at,
            "value": drift,
            "meta": json.dumps(meta),
        },
    )


def _source_id(session: Session, name: str) -> int:
    return session.execute(
        text("SELECT id FROM sources WHERE name = :n"), {"n": name}
    ).scalar_one()


# ---------------------------------------------------------------------
# Status-code contract
# ---------------------------------------------------------------------


@_db_required
class TestDriftStatusCodes:
    def test_unknown_slug_404(self, client: TestClient) -> None:
        resp = client.get("/items/does-not-exist/drift")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_known_item_no_rows_returns_200_empty_pairs(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Deep-tier item with no drift cycles → 200 + empty pairs.
        The sentinel here is technically orphan (not in YAML); the
        contract is the same — empty pairs, 200 not 404."""
        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == _SENTINEL_SLUG
        assert body["pairs"] == []
        # Sentinel isn't in YAML → orphan.
        assert body["tier"] == "substrate"


# ---------------------------------------------------------------------
# Pair counts: zero / one / two
# ---------------------------------------------------------------------


@_db_required
class TestDriftPairShapes:
    def test_two_pair_steady_state(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Both pairs present, both fresh → 2 entries in pairs[]."""
        item_id = sentinel_item
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            sk_id = _source_id(session, "skinport")
            psk_id = _source_id(session, "pricempire_skinport")
            dm_id = _source_id(session, "dmarket")
            pdm_id = _source_id(session, "pricempire_dmarket")
            _insert_drift_verdict(
                session,
                item_id=item_id,
                computed_at=now,
                verdict="no_drift",
                drift=Decimal("-0.0123"),
                source_a_id=sk_id,
                source_a_name="skinport",
                source_b_id=psk_id,
                source_b_name="pricempire_skinport",
            )
            _insert_drift_verdict(
                session,
                item_id=item_id,
                computed_at=now,
                verdict="drift_alert",
                drift=Decimal("0.1532"),
                source_a_id=dm_id,
                source_a_name="dmarket",
                source_b_id=pdm_id,
                source_b_name="pricempire_dmarket",
                curated_price="31.41",
                pricempire_price="27.24",
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["pairs"]) == 2
        verdicts_by_pair = {
            (p["source_a"], p["source_b"]): p["verdict"]
            for p in body["pairs"]
        }
        assert verdicts_by_pair[
            ("skinport", "pricempire_skinport")
        ] == "no_drift"
        assert verdicts_by_pair[
            ("dmarket", "pricempire_dmarket")
        ] == "drift_alert"

    @pytest.mark.parametrize(
        "missing_pair",
        [
            ("dmarket", "pricempire_dmarket"),
            ("skinport", "pricempire_skinport"),
        ],
        ids=["dmarket_missing", "skinport_missing"],
    )
    def test_one_pair_only_middle_state(
        self,
        client: TestClient,
        sentinel_item,
        missing_pair: tuple[str, str],
    ) -> None:
        """A partially populated item returns one drift pair cleanly."""
        item_id = sentinel_item
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            sk_id = _source_id(session, "skinport")
            psk_id = _source_id(session, "pricempire_skinport")
            dm_id = _source_id(session, "dmarket")
            pdm_id = _source_id(session, "pricempire_dmarket")
            # Insert ONE pair only.
            if missing_pair == ("dmarket", "pricempire_dmarket"):
                _insert_drift_verdict(
                    session,
                    item_id=item_id,
                    computed_at=now,
                    verdict="no_drift",
                    drift=Decimal("0.0050"),
                    source_a_id=sk_id,
                    source_a_name="skinport",
                    source_b_id=psk_id,
                    source_b_name="pricempire_skinport",
                )
                present_pair = ("skinport", "pricempire_skinport")
            else:
                _insert_drift_verdict(
                    session,
                    item_id=item_id,
                    computed_at=now,
                    verdict="no_drift",
                    drift=Decimal("0.0050"),
                    source_a_id=dm_id,
                    source_a_name="dmarket",
                    source_b_id=pdm_id,
                    source_b_name="pricempire_dmarket",
                )
                present_pair = ("dmarket", "pricempire_dmarket")
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["pairs"]) == 1
        pair = body["pairs"][0]
        assert (pair["source_a"], pair["source_b"]) == present_pair
        assert pair["verdict"] == "no_drift"


# ---------------------------------------------------------------------
# DISTINCT ON: latest row per pair wins
# ---------------------------------------------------------------------


@_db_required
class TestDriftDistinctOn:
    def test_returns_latest_row_per_pair(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Three cycles of skinport-pair verdicts; endpoint surfaces
        only the latest. Verifies the DISTINCT ON
        (source_a_id, source_b_id) ORDER BY computed_at DESC pattern."""
        item_id = sentinel_item
        now = datetime.now(UTC)
        engine = get_engine()
        with Session(engine) as session:
            sk_id = _source_id(session, "skinport")
            psk_id = _source_id(session, "pricempire_skinport")
            for minutes_ago, verdict, drift in [
                (60, "drift_alert", Decimal("0.1500")),
                (30, "no_drift", Decimal("0.0500")),
                (0, "no_drift", Decimal("0.0100")),  # latest
            ]:
                _insert_drift_verdict(
                    session,
                    item_id=item_id,
                    computed_at=now - timedelta(minutes=minutes_ago),
                    verdict=verdict,
                    drift=drift,
                    source_a_id=sk_id,
                    source_a_name="skinport",
                    source_b_id=psk_id,
                    source_b_name="pricempire_skinport",
                )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["pairs"]) == 1
        assert body["pairs"][0]["verdict"] == "no_drift"
        assert body["pairs"][0]["drift"] == "0.0100"


# ---------------------------------------------------------------------
# Tier shaping via reload()
# ---------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def _yaml_for_tier(tier: str) -> str:
    """Synthetic YAML where the sentinel is the only item, classified
    as the given tier. Includes a minimal sources block so
    load_watchlist's schema_version + required-keys checks pass."""
    return f"""\
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
    wear: "Factory New"
    tier: {tier}
"""


@_db_required
class TestDriftTierShaping:
    def test_curated_tier_with_verdicts(
        self, client: TestClient, sentinel_item, tmp_path: Path
    ) -> None:
        """When the sentinel is classified as deep in YAML, the
        endpoint returns tier=deep and includes drift verdicts."""
        yaml_path = tmp_path / "watchlist.yaml"
        _write_yaml(yaml_path, _yaml_for_tier("curated"))
        watchlist_tiers.reload(yaml_path)

        item_id = sentinel_item
        engine = get_engine()
        with Session(engine) as session:
            sk_id = _source_id(session, "skinport")
            psk_id = _source_id(session, "pricempire_skinport")
            _insert_drift_verdict(
                session,
                item_id=item_id,
                computed_at=datetime.now(UTC),
                verdict="no_drift",
                drift=Decimal("0.0010"),
                source_a_id=sk_id,
                source_a_name="skinport",
                source_b_id=psk_id,
                source_b_name="pricempire_skinport",
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "curated"
        assert len(body["pairs"]) == 1

    def test_featured_tier_returns_empty_pairs(
        self, client: TestClient, sentinel_item, tmp_path: Path
    ) -> None:
        """The broad-tier contract pinned: drift detection skips
        broad tier by construction, so the endpoint returns
        tier=broad with empty pairs even when the detector hasn't
        written rows (which it wouldn't, because broad isn't in the
        deep_set). Today no broad-tier items exist in production;
        this test guarantees future broad-tier population doesn't
        surprise the bot."""
        yaml_path = tmp_path / "watchlist.yaml"
        _write_yaml(yaml_path, _yaml_for_tier("featured"))
        watchlist_tiers.reload(yaml_path)

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "featured"
        assert body["pairs"] == []

    def test_substrate_tier_returns_empty_pairs(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Sentinel is not in the default YAML, so tier=orphan and
        pairs=[]. No tmp YAML needed — the default already lacks
        the sentinel."""
        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "substrate"
        assert body["pairs"] == []


# ---------------------------------------------------------------------
# Payload fidelity: meta_info → DriftPairVerdict round-trip
# ---------------------------------------------------------------------


@_db_required
class TestDriftPayloadShape:
    def test_full_payload_fields_present(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Verify every documented DriftPairVerdict field round-trips
        cleanly from meta_info JSONB through the route to the wire."""
        item_id = sentinel_item
        now = datetime.now(UTC).replace(microsecond=0)
        engine = get_engine()
        with Session(engine) as session:
            sk_id = _source_id(session, "skinport")
            psk_id = _source_id(session, "pricempire_skinport")
            _insert_drift_verdict(
                session,
                item_id=item_id,
                computed_at=now,
                verdict="drift_alert",
                drift=Decimal("0.1234"),
                source_a_id=sk_id,
                source_a_name="skinport",
                source_b_id=psk_id,
                source_b_name="pricempire_skinport",
                classification="pattern_seed",
                threshold_multiplier=2.0,
                threshold_used="0.20",
                curated_price="123.45",
                pricempire_price="100.00",
                curated_age_min=3.5,
                pricempire_age_min=1.5,
                note="Sport Gloves Vice: 2.0x threshold (pattern-seed)",
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["pairs"]) == 1
        p = body["pairs"][0]

        assert p["source_a"] == "skinport"
        assert p["source_b"] == "pricempire_skinport"
        assert p["verdict"] == "drift_alert"
        assert p["drift"] == "0.1234"
        assert p["threshold_used"] == "0.20"
        assert p["classification"] == "pattern_seed"
        assert p["threshold_multiplier"] == 2.0
        assert p["curated_price"] == "123.45"
        assert p["pricempire_price"] == "100.00"
        assert p["curated_age_min"] == 3.5
        assert p["pricempire_age_min"] == 1.5
        assert p["note"] == (
            "Sport Gloves Vice: 2.0x threshold (pattern-seed)"
        )
        # Timestamps should be ISO-parseable.
        datetime.fromisoformat(p["computed_at"])
        datetime.fromisoformat(p["curated_last_polled_at"])
        datetime.fromisoformat(p["pricempire_last_polled_at"])

    def test_no_comparable_data_drift_is_null(
        self, client: TestClient, sentinel_item
    ) -> None:
        """Non-numeric verdicts (no_comparable_data, pattern_skip,
        stale_*) carry drift=None on the wire."""
        item_id = sentinel_item
        engine = get_engine()
        with Session(engine) as session:
            sk_id = _source_id(session, "skinport")
            psk_id = _source_id(session, "pricempire_skinport")
            _insert_drift_verdict(
                session,
                item_id=item_id,
                computed_at=datetime.now(UTC),
                verdict="no_comparable_data",
                drift=None,
                source_a_id=sk_id,
                source_a_name="skinport",
                source_b_id=psk_id,
                source_b_name="pricempire_skinport",
                curated_price=None,
                pricempire_price=None,
            )
            session.commit()

        resp = client.get(f"/items/{_SENTINEL_SLUG}/drift")
        body = resp.json()
        assert body["pairs"][0]["drift"] is None
        assert body["pairs"][0]["curated_price"] is None
        assert body["pairs"][0]["pricempire_price"] is None


# ---------------------------------------------------------------------
# Auth coverage (sanity)
# ---------------------------------------------------------------------


class TestDriftAuth:
    def test_missing_authorization_header_401(self) -> None:
        c = TestClient(app)
        resp = c.get(f"/items/{_SENTINEL_SLUG}/drift")
        assert resp.status_code == 401
