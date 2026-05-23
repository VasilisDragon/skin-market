"""Discord entitlement API tests."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from db.connection import get_engine

_TEST_TOKEN = "test-token-deadbeefcafebabe1234567890"
_DISCORD_USER_ID = "entitlement-test-user"


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


@pytest.fixture(autouse=True)
def _clean_entitlement():
    engine = get_engine()
    with Session(engine) as session:
        session.execute(
            text("DELETE FROM discord_entitlements WHERE discord_user_id = :u"),
            {"u": _DISCORD_USER_ID},
        )
        session.commit()
    yield
    with Session(engine) as session:
        session.execute(
            text("DELETE FROM discord_entitlements WHERE discord_user_id = :u"),
            {"u": _DISCORD_USER_ID},
        )
        session.commit()


@pytest.fixture
def client() -> TestClient:
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
    return c


def test_default_entitlement_uses_environment_fallbacks(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PRICE_ALERT_MAX_ACTIVE_PER_USER", "9")
    monkeypatch.setenv("PORTFOLIO_SNAPSHOT_MAX_DAILY_PER_USER", "8")
    monkeypatch.setenv("SIGNAL_SUBSCRIPTION_MAX_ACTIVE_PER_USER", "2")
    monkeypatch.setenv("PORTFOLIO_MONITOR_MAX_ACTIVE_PER_USER", "4")

    response = client.get(f"/entitlements/discord/{_DISCORD_USER_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "default"
    assert body["tier"] == "default"
    assert body["quotas"] == {
        "active_price_alerts": 9,
        "portfolio_snapshots_per_day": 8,
        "signal_subscriptions": 2,
        "portfolio_monitors": 4,
    }


def test_update_entitlement_returns_tier_quotas(client) -> None:
    updated = client.put(
        f"/entitlements/discord/{_DISCORD_USER_ID}",
        json={"tier": "trader", "status": "active"},
    )
    fetched = client.get(f"/entitlements/discord/{_DISCORD_USER_ID}")

    assert updated.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["source"] == "stored"
    assert fetched.json()["tier"] == "trader"
    assert fetched.json()["quotas"] == {
        "active_price_alerts": 25,
        "portfolio_snapshots_per_day": 20,
        "signal_subscriptions": 5,
        "portfolio_monitors": 3,
    }


def test_disabled_entitlement_has_zero_quotas(client) -> None:
    response = client.put(
        f"/entitlements/discord/{_DISCORD_USER_ID}",
        json={"tier": "pro", "status": "disabled"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert response.json()["quotas"] == {
        "active_price_alerts": 0,
        "portfolio_snapshots_per_day": 0,
        "signal_subscriptions": 0,
        "portfolio_monitors": 0,
    }
