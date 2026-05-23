"""Recurring portfolio monitor API tests."""

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
_DISCORD_USER_ID = "portfolio-monitor-test-user"
_DISCORD_CHANNEL_ID = "9876543210"
_STEAM_ID = "76561199276192848"
_INVENTORY_URL = f"https://steamcommunity.com/profiles/{_STEAM_ID}/inventory/"
_SENTINEL_NAME = "__PortfolioMonitorTest__ | Sentinel (Field-Tested)"
_SENTINEL_SLUG = "portfoliomonitortest-sentinel-field-tested"


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
def portfolio_monitor_item(monkeypatch):
    from api.routes import asset_valuation as asset_valuation_route

    monkeypatch.setattr(
        asset_valuation_route,
        "fetch_pricempire_inventory",
        lambda steam_id: {
            "items": [
                {
                    "asset_id": "asset-1",
                    "float_value": "0.123456",
                    "paint_seed": 321,
                    "item": {
                        "market_hash_name": _SENTINEL_NAME,
                        "paint_id": 999,
                    },
                    "stickers": [],
                }
            ]
        },
    )

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
        _cleanup(session, item_id)
        source_id = _ensure_source(session, "skinport", "usd")
        _insert_price(session, item_id, source_id, now, "100.00")
        session.commit()

        yield item_id

        _cleanup(session, item_id)
        session.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
        session.commit()


def _cleanup(session: Session, item_id: uuid.UUID) -> None:
    session.execute(
        text("DELETE FROM portfolio_monitors WHERE discord_user_id = :u"),
        {"u": _DISCORD_USER_ID},
    )
    session.execute(
        text(
            """
            DELETE FROM portfolio_snapshots
            WHERE discord_user_id = :discord_user_id
              AND steam_id = :steam_id
            """
        ),
        {"discord_user_id": _DISCORD_USER_ID, "steam_id": _STEAM_ID},
    )
    session.execute(
        text("DELETE FROM discord_entitlements WHERE discord_user_id = :u"),
        {"u": _DISCORD_USER_ID},
    )
    session.execute(text("DELETE FROM prices WHERE item_id = :i"), {"i": item_id})
    session.execute(text("DELETE FROM observation_log WHERE item_id = :i"), {"i": item_id})


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


def _insert_price(
    session: Session,
    item_id: uuid.UUID,
    source_id: int,
    observed_at: datetime,
    price: str,
) -> None:
    session.execute(
        pg_insert(Price)
        .values(
            item_id=item_id,
            source_id=source_id,
            timestamp=observed_at,
            price=Decimal(price),
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
        {"item_id": item_id, "source_id": source_id, "observed_at": observed_at},
    )


def _monitor_payload(**overrides) -> dict:
    payload = {
        "discord_user_id": _DISCORD_USER_ID,
        "discord_channel_id": _DISCORD_CHANNEL_ID,
        "inventory_url": _INVENTORY_URL,
        "interval_minutes": 60,
        "change_threshold_pct": "5.00",
        "timezone_offset_minutes": 0,
    }
    payload.update(overrides)
    return payload


def test_create_list_and_cancel_portfolio_monitor(
    client,
    portfolio_monitor_item,
) -> None:
    del portfolio_monitor_item
    created = client.post("/portfolio/monitors", json=_monitor_payload())
    assert created.status_code == 200
    monitor = created.json()
    assert monitor["status"] == "active"
    assert monitor["change_threshold_pct"] == "5.00"

    listed = client.get(
        "/portfolio/monitors",
        params={"discord_user_id": _DISCORD_USER_ID},
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [monitor["id"]]

    cancelled = client.post(
        f"/portfolio/monitors/{monitor['id']}/cancel",
        json={"discord_user_id": _DISCORD_USER_ID},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_portfolio_monitor_evaluate_and_delivery_ack(
    client,
    portfolio_monitor_item,
) -> None:
    del portfolio_monitor_item
    created = client.post("/portfolio/monitors", json=_monitor_payload()).json()

    evaluated = client.post("/portfolio/monitors/evaluate", json={"limit": 10})
    assert evaluated.status_code == 200
    due = [
        row for row in evaluated.json()["due"] if row["monitor"]["id"] == created["id"]
    ]
    assert len(due) == 1
    assert due[0]["event_type"] == "initial_snapshot"
    snapshot_id = due[0]["snapshot_result"]["snapshot"]["id"]

    delivered = client.post(
        f"/portfolio/monitors/{created['id']}/delivery",
        json={"delivered": True, "snapshot_id": snapshot_id},
    )

    assert delivered.status_code == 200
    assert delivered.json()["last_snapshot_id"] == snapshot_id
    assert delivered.json()["last_sent_at"] is not None


def test_portfolio_monitor_quiet_hours_skip_due(
    client,
    portfolio_monitor_item,
) -> None:
    del portfolio_monitor_item
    now_hour = datetime.now(UTC).hour
    created = client.post(
        "/portfolio/monitors",
        json=_monitor_payload(
            quiet_start_hour=now_hour,
            quiet_end_hour=(now_hour + 1) % 24,
        ),
    ).json()

    evaluated = client.post("/portfolio/monitors/evaluate", json={"limit": 10})

    assert created["status"] == "active"
    assert created["id"] not in [
        row["monitor"]["id"] for row in evaluated.json()["due"]
    ]
