"""Persistent price-alert API tests."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from db.connection import get_engine
from db.models import Item, Price

_TEST_TOKEN = "test-token-deadbeefcafebabe1234567890"
_SENTINEL_NAME = "__AlertTest__ | Sentinel (Field-Tested)"
_SENTINEL_SLUG = "alerttest-sentinel-field-tested"
_DISCORD_USER_ID = "1234567890"
_DISCORD_CHANNEL_ID = "9876543210"


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


@pytest.fixture(autouse=True)
def _set_api_token(monkeypatch):
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)


@pytest.fixture
def client() -> TestClient:
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
    return c


@pytest.fixture
def alert_item():
    engine = get_engine()
    item_id = uuid.uuid4()
    now = datetime.now(UTC)
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
            {"id": item_id, "name": _SENTINEL_NAME, "slug": _SENTINEL_SLUG},
        )
        item_id = session.execute(
            select(Item.id).where(Item.market_hash_name == _SENTINEL_NAME)
        ).scalar_one()
        session.execute(text("DELETE FROM price_alerts WHERE item_id = :i"), {"i": item_id})
        session.execute(text("DELETE FROM prices WHERE item_id = :i"), {"i": item_id})
        session.execute(text("DELETE FROM observation_log WHERE item_id = :i"), {"i": item_id})
        source_id = _ensure_source(session, "skinport", "usd")
        session.execute(
            pg_insert(Price)
            .values(
                item_id=item_id,
                source_id=source_id,
                timestamp=now,
                price=Decimal("24.00"),
                volume=7,
                currency="USD",
                raw_response={"synthetic": True},
            )
            .on_conflict_do_nothing(
                index_elements=["item_id", "source_id", "timestamp"]
            )
        )
        session.execute(
            text(
                """
                INSERT INTO observation_log
                    (item_id, source_id, last_observed_at)
                VALUES (:item_id, :source_id, :observed_at)
                ON CONFLICT (item_id, source_id)
                DO UPDATE SET last_observed_at = EXCLUDED.last_observed_at
                """
            ),
            {"item_id": item_id, "source_id": source_id, "observed_at": now},
        )
        session.commit()

        yield item_id

        session.execute(text("DELETE FROM price_alerts WHERE item_id = :i"), {"i": item_id})
        session.execute(text("DELETE FROM prices WHERE item_id = :i"), {"i": item_id})
        session.execute(text("DELETE FROM observation_log WHERE item_id = :i"), {"i": item_id})
        session.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
        session.commit()


def _ensure_source(session: Session, name: str, denomination: str) -> int:
    return session.execute(
        text(
            """
            INSERT INTO sources (
                name, base_url, rate_limit_per_minute, enabled, denomination,
                interval_minutes, per_item_delay_seconds
            )
            VALUES (:name, NULL, NULL, TRUE, :denomination, 30, 5)
            ON CONFLICT (name)
            DO UPDATE SET enabled = TRUE, denomination = EXCLUDED.denomination
            RETURNING id
            """
        ),
        {"name": name, "denomination": denomination},
    ).scalar_one()


def test_create_list_and_cancel_price_alert(client, alert_item) -> None:
    del alert_item
    created = client.post(
        "/alerts/price",
        json={
            "discord_user_id": _DISCORD_USER_ID,
            "discord_channel_id": _DISCORD_CHANNEL_ID,
            "slug": _SENTINEL_SLUG,
            "direction": "at_or_below",
            "threshold_price": "25.00",
            "currency": "usd",
        },
    )
    assert created.status_code == 200
    alert = created.json()
    assert alert["status"] == "active"
    assert alert["display_name"] == _SENTINEL_NAME

    listed = client.get(
        "/alerts/price",
        params={"discord_user_id": _DISCORD_USER_ID},
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [alert["id"]]

    cancelled = client.post(
        f"/alerts/price/{alert['id']}/cancel",
        json={"discord_user_id": _DISCORD_USER_ID},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_evaluate_price_alerts_marks_triggered(client, alert_item) -> None:
    del alert_item
    created = client.post(
        "/alerts/price",
        json={
            "discord_user_id": _DISCORD_USER_ID,
            "discord_channel_id": _DISCORD_CHANNEL_ID,
            "slug": _SENTINEL_SLUG,
            "direction": "at_or_below",
            "threshold_price": "25.00",
            "currency": "usd",
        },
    ).json()

    evaluated = client.post("/alerts/price/evaluate", json={"limit": 10})

    assert evaluated.status_code == 200
    body = evaluated.json()
    assert body["checked_count"] >= 1
    triggered = [row for row in body["triggered"] if row["id"] == created["id"]]
    assert len(triggered) == 1
    assert triggered[0]["status"] == "triggered"
    assert triggered[0]["trigger_price"] == "24.00"
    assert triggered[0]["trigger_source"] == "skinport"
