"""Operator-managed Discord entitlement routes."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.entitlements import effective_entitlement_policy
from api.schemas import (
    DiscordEntitlementResponse,
    DiscordEntitlementUpdateRequest,
)
from db.connection import get_engine
from db.models import DiscordEntitlement

router = APIRouter(tags=["entitlements"])

DEFAULT_PORTFOLIO_SNAPSHOT_MAX_DAILY_PER_USER = 10


@router.get(
    "/entitlements/discord/{discord_user_id}",
    response_model=DiscordEntitlementResponse,
)
def get_discord_entitlement(discord_user_id: str) -> DiscordEntitlementResponse:
    engine = get_engine()
    with Session(engine) as session:
        policy = effective_entitlement_policy(
            session,
            discord_user_id,
            default_active_price_alerts=_default_active_price_alerts(),
            default_portfolio_snapshots_per_day=(
                portfolio_snapshot_max_daily_per_user()
            ),
        )
        row = session.execute(
            select(DiscordEntitlement).where(
                DiscordEntitlement.discord_user_id == discord_user_id
            )
        ).scalar_one_or_none()
        created_at = row.created_at if row is not None else None
        updated_at = row.updated_at if row is not None else None

    return DiscordEntitlementResponse(
        discord_user_id=discord_user_id,
        tier=policy.tier,  # type: ignore[arg-type]
        status=policy.status,  # type: ignore[arg-type]
        source=policy.source,  # type: ignore[arg-type]
        created_at=created_at,
        updated_at=updated_at,
        quotas=policy.quotas(),
    )


@router.put(
    "/entitlements/discord/{discord_user_id}",
    response_model=DiscordEntitlementResponse,
)
def update_discord_entitlement(
    discord_user_id: str,
    req: DiscordEntitlementUpdateRequest,
) -> DiscordEntitlementResponse:
    engine = get_engine()
    now = datetime.now(UTC)
    with Session(engine) as session:
        row = session.execute(
            select(DiscordEntitlement).where(
                DiscordEntitlement.discord_user_id == discord_user_id
            )
        ).scalar_one_or_none()
        if row is None:
            row = DiscordEntitlement(
                discord_user_id=discord_user_id,
                tier=req.tier,
                status=req.status,
                updated_at=now,
            )
            session.add(row)
        else:
            row.tier = req.tier
            row.status = req.status
            row.updated_at = now
        session.commit()
        session.refresh(row)
        policy = effective_entitlement_policy(
            session,
            discord_user_id,
            default_active_price_alerts=_default_active_price_alerts(),
            default_portfolio_snapshots_per_day=(
                portfolio_snapshot_max_daily_per_user()
            ),
        )
        return DiscordEntitlementResponse(
            discord_user_id=discord_user_id,
            tier=policy.tier,  # type: ignore[arg-type]
            status=policy.status,  # type: ignore[arg-type]
            source=policy.source,  # type: ignore[arg-type]
            created_at=row.created_at,
            updated_at=row.updated_at,
            quotas=policy.quotas(),
        )


def portfolio_snapshot_max_daily_per_user() -> int:
    return int(
        os.environ.get(
            "PORTFOLIO_SNAPSHOT_MAX_DAILY_PER_USER",
            DEFAULT_PORTFOLIO_SNAPSHOT_MAX_DAILY_PER_USER,
        )
    )


def _default_active_price_alerts() -> int:
    return int(os.environ.get("PRICE_ALERT_MAX_ACTIVE_PER_USER", 25))
