"""Discord entitlement and quota policy helpers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import DiscordEntitlement

TIER_QUOTAS: dict[str, dict[str, int]] = {
    "free": {
        "active_price_alerts": 3,
        "portfolio_snapshots_per_day": 3,
    },
    "trader": {
        "active_price_alerts": 25,
        "portfolio_snapshots_per_day": 20,
    },
    "pro": {
        "active_price_alerts": 100,
        "portfolio_snapshots_per_day": 100,
    },
}


@dataclass(frozen=True)
class EntitlementPolicy:
    discord_user_id: str
    tier: str
    status: str
    source: str
    active_price_alerts: int
    portfolio_snapshots_per_day: int

    def quotas(self) -> dict[str, int]:
        return {
            "active_price_alerts": self.active_price_alerts,
            "portfolio_snapshots_per_day": self.portfolio_snapshots_per_day,
        }


def effective_entitlement_policy(
    session: Session,
    discord_user_id: str,
    *,
    default_active_price_alerts: int,
    default_portfolio_snapshots_per_day: int,
) -> EntitlementPolicy:
    row = session.execute(
        select(DiscordEntitlement).where(
            DiscordEntitlement.discord_user_id == discord_user_id
        )
    ).scalar_one_or_none()
    if row is None:
        return EntitlementPolicy(
            discord_user_id=discord_user_id,
            tier="default",
            status="active",
            source="default",
            active_price_alerts=default_active_price_alerts,
            portfolio_snapshots_per_day=default_portfolio_snapshots_per_day,
        )
    if row.status != "active":
        return EntitlementPolicy(
            discord_user_id=discord_user_id,
            tier=row.tier,
            status=row.status,
            source="stored",
            active_price_alerts=0,
            portfolio_snapshots_per_day=0,
        )

    quotas = TIER_QUOTAS[row.tier]
    return EntitlementPolicy(
        discord_user_id=discord_user_id,
        tier=row.tier,
        status=row.status,
        source="stored",
        active_price_alerts=quotas["active_price_alerts"],
        portfolio_snapshots_per_day=quotas["portfolio_snapshots_per_day"],
    )
