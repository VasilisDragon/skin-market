"""Recurring signal digest subscription API tests."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from db.connection import get_engine
from db.models import Item

_TEST_TOKEN = "test-token-deadbeefcafebabe1234567890"
_DISCORD_USER_ID = "signal-sub-test-user"
_DISCORD_CHANNEL_ID = "9876543210"
_SENTINEL_NAME = "__SignalSubTest__ | Sentinel (Field-Tested)"
_SENTINEL_SLUG = "signalsubtest-sentinel-field-tested"


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
def signal_item():
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
            {"id": item_id, "name": _SENTINEL_NAME, "slug": _SENTINEL_SLUG},
        )
        item_id = session.execute(
            select(Item.id).where(Item.market_hash_name == _SENTINEL_NAME)
        ).scalar_one()
        _cleanup(session, item_id)
        session.commit()

        yield item_id

        _cleanup(session, item_id)
        session.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
        session.commit()


def _cleanup(session: Session, item_id: uuid.UUID) -> None:
    session.execute(
        text(
            "DELETE FROM signal_subscriptions WHERE discord_user_id = :u"
        ),
        {"u": _DISCORD_USER_ID},
    )
    session.execute(
        text(
            "DELETE FROM insights "
            "WHERE item_id = :i AND insight_type IN "
            "('cross_source_divergence', 'volume_anomaly')"
        ),
        {"i": item_id},
    )
    session.execute(
        text("DELETE FROM discord_entitlements WHERE discord_user_id = :u"),
        {"u": _DISCORD_USER_ID},
    )


def _seed_signal(item_id: uuid.UUID, z_score: str = "99.00") -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO insights (
                    item_id, computed_at, insight_type, value, meta_info
                )
                VALUES (
                    :i, :t, 'cross_source_divergence', :z,
                    CAST(:m AS jsonb)
                )
                """
            ),
            {
                "i": item_id,
                "t": datetime.now(UTC),
                "z": z_score,
                "m": json.dumps(
                    {
                        "source_a_id": "1",
                        "source_b_id": "27",
                        "observed_spread": "0.42",
                        "baseline_mean": "0.10",
                    }
                ),
            },
        )
        session.commit()


def _subscription_payload(**overrides) -> dict:
    payload = {
        "discord_user_id": _DISCORD_USER_ID,
        "discord_channel_id": _DISCORD_CHANNEL_ID,
        "hours": 6,
        "limit": 4,
        "threshold_z": "3.00",
        "interval_minutes": 15,
        "timezone_offset_minutes": 0,
    }
    payload.update(overrides)
    return payload


def test_create_list_and_cancel_signal_subscription(client, signal_item) -> None:
    del signal_item
    created = client.post(
        "/signals/subscriptions",
        json=_subscription_payload(),
    )
    assert created.status_code == 200
    sub = created.json()
    assert sub["status"] == "active"
    assert sub["threshold_z"] == "3.00"

    listed = client.get(
        "/signals/subscriptions",
        params={"discord_user_id": _DISCORD_USER_ID},
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [sub["id"]]

    cancelled = client.post(
        f"/signals/subscriptions/{sub['id']}/cancel",
        json={"discord_user_id": _DISCORD_USER_ID},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_signal_subscription_quota(client, signal_item, monkeypatch) -> None:
    del signal_item
    monkeypatch.setenv("SIGNAL_SUBSCRIPTION_MAX_ACTIVE_PER_USER", "1")

    first = client.post("/signals/subscriptions", json=_subscription_payload())
    second = client.post("/signals/subscriptions", json=_subscription_payload())

    assert first.status_code == 200
    assert second.status_code == 409
    assert "quota" in second.json()["detail"].lower()


def test_signal_subscription_evaluate_and_delivery_ack(
    client,
    signal_item,
) -> None:
    _seed_signal(signal_item)
    created = client.post(
        "/signals/subscriptions",
        json=_subscription_payload(threshold_z="90.00"),
    ).json()

    evaluated = client.post("/signals/subscriptions/evaluate", json={"limit": 10})
    assert evaluated.status_code == 200
    body = evaluated.json()
    due = [row for row in body["due"] if row["subscription"]["id"] == created["id"]]
    assert len(due) == 1
    assert due[0]["digest"]["returned_count"] == 1
    assert due[0]["digest_fingerprint"]

    delivered = client.post(
        f"/signals/subscriptions/{created['id']}/delivery",
        json={
            "delivered": True,
            "digest_fingerprint": due[0]["digest_fingerprint"],
        },
    )
    assert delivered.status_code == 200
    assert delivered.json()["last_sent_at"] is not None
    assert delivered.json()["last_digest_fingerprint"] == due[0]["digest_fingerprint"]

    final = client.post("/signals/subscriptions/evaluate", json={"limit": 10})
    assert created["id"] not in [
        row["subscription"]["id"] for row in final.json()["due"]
    ]


def test_signal_subscription_quiet_hours_skip_due(client, signal_item) -> None:
    _seed_signal(signal_item)
    now_hour = datetime.now(UTC).hour
    created = client.post(
        "/signals/subscriptions",
        json=_subscription_payload(
            threshold_z="90.00",
            quiet_start_hour=now_hour,
            quiet_end_hour=(now_hour + 1) % 24,
        ),
    ).json()

    evaluated = client.post("/signals/subscriptions/evaluate", json={"limit": 10})

    assert created["status"] == "active"
    assert created["id"] not in [
        row["subscription"]["id"] for row in evaluated.json()["due"]
    ]
