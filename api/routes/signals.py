"""Recurring Discord signal digest subscriptions."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.entitlements import effective_entitlement_policy
from api.routes.insights import build_signal_digest
from api.schemas import (
    SignalSubscriptionCancelRequest,
    SignalSubscriptionCreateRequest,
    SignalSubscriptionDeliveryRequest,
    SignalSubscriptionEvaluateRequest,
    SignalSubscriptionEvaluateResponse,
    SignalSubscriptionResponse,
)
from db.connection import get_engine
from db.models import SignalSubscription

router = APIRouter(tags=["signal subscriptions"])

DEFAULT_SIGNAL_SUBSCRIPTION_MAX_ACTIVE_PER_USER = 1


@router.post(
    "/signals/subscriptions",
    response_model=SignalSubscriptionResponse,
)
def create_signal_subscription(
    req: SignalSubscriptionCreateRequest,
) -> SignalSubscriptionResponse:
    engine = get_engine()
    with Session(engine) as session:
        active_count = session.scalar(
            select(func.count())
            .select_from(SignalSubscription)
            .where(
                SignalSubscription.discord_user_id == req.discord_user_id,
                SignalSubscription.status == "active",
            )
        )
        policy = effective_entitlement_policy(
            session,
            req.discord_user_id,
            default_active_price_alerts=25,
            default_portfolio_snapshots_per_day=10,
            default_signal_subscriptions=signal_subscription_max_active_per_user(),
        )
        max_active = policy.signal_subscriptions
        if active_count is not None and active_count >= max_active:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Active signal subscription quota reached "
                    f"({active_count}/{max_active})."
                ),
            )

        sub = SignalSubscription(
            discord_user_id=req.discord_user_id,
            discord_channel_id=req.discord_channel_id,
            hours=req.hours,
            limit=req.limit,
            threshold_z=req.threshold_z,
            interval_minutes=req.interval_minutes,
            quiet_start_hour=req.quiet_start_hour,
            quiet_end_hour=req.quiet_end_hour,
            timezone_offset_minutes=req.timezone_offset_minutes,
            status="active",
        )
        session.add(sub)
        session.commit()
        session.refresh(sub)
        return _subscription_response(sub)


@router.get(
    "/signals/subscriptions",
    response_model=list[SignalSubscriptionResponse],
)
def list_signal_subscriptions(
    discord_user_id: str = Query(...),
    include_inactive: bool = False,
) -> list[SignalSubscriptionResponse]:
    stmt = (
        select(SignalSubscription)
        .where(SignalSubscription.discord_user_id == discord_user_id)
        .order_by(SignalSubscription.created_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(SignalSubscription.status == "active")

    engine = get_engine()
    with Session(engine) as session:
        rows = session.execute(stmt).scalars().all()
    return [_subscription_response(row) for row in rows]


@router.post(
    "/signals/subscriptions/{subscription_id}/cancel",
    response_model=SignalSubscriptionResponse,
)
def cancel_signal_subscription(
    subscription_id: str,
    req: SignalSubscriptionCancelRequest,
) -> SignalSubscriptionResponse:
    try:
        parsed_id = uuid.UUID(subscription_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscription not found") from exc

    engine = get_engine()
    with Session(engine) as session:
        sub = session.execute(
            select(SignalSubscription).where(
                SignalSubscription.id == parsed_id,
                SignalSubscription.discord_user_id == req.discord_user_id,
            )
        ).scalar_one_or_none()
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if sub.status == "active":
            sub.status = "cancelled"
            session.commit()
            session.refresh(sub)
        return _subscription_response(sub)


@router.post(
    "/signals/subscriptions/evaluate",
    response_model=SignalSubscriptionEvaluateResponse,
)
def evaluate_signal_subscriptions(
    req: SignalSubscriptionEvaluateRequest,
) -> SignalSubscriptionEvaluateResponse:
    now = datetime.now(UTC)
    checked_count = 0
    due: list[dict[str, Any]] = []

    engine = get_engine()
    with Session(engine) as session:
        subscriptions = session.execute(
            select(SignalSubscription)
            .where(SignalSubscription.status == "active")
            .order_by(SignalSubscription.created_at)
            .limit(req.limit)
        ).scalars().all()

        for sub in subscriptions:
            if _is_quiet_now(sub, now):
                continue
            if not _is_due(sub, now):
                continue
            checked_count += 1
            sub.last_checked_at = now
            digest = build_signal_digest(
                hours=sub.hours,
                limit=sub.limit,
                min_abs_z=float(sub.threshold_z),
            )
            if digest.returned_count == 0:
                continue
            digest_data = digest.model_dump(mode="json")
            fingerprint = _digest_fingerprint(digest_data)
            if fingerprint == sub.last_digest_fingerprint:
                continue
            due.append(
                {
                    "subscription": _subscription_response(sub),
                    "digest": digest_data,
                    "digest_fingerprint": fingerprint,
                }
            )

        session.commit()

    return SignalSubscriptionEvaluateResponse(
        checked_count=checked_count,
        due=due,
    )


@router.post(
    "/signals/subscriptions/{subscription_id}/delivery",
    response_model=SignalSubscriptionResponse,
)
def record_signal_subscription_delivery(
    subscription_id: str,
    req: SignalSubscriptionDeliveryRequest,
) -> SignalSubscriptionResponse:
    try:
        parsed_id = uuid.UUID(subscription_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscription not found") from exc

    now = datetime.now(UTC)
    engine = get_engine()
    with Session(engine) as session:
        sub = session.execute(
            select(SignalSubscription).where(SignalSubscription.id == parsed_id)
        ).scalar_one_or_none()
        if sub is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if sub.status != "active":
            raise HTTPException(
                status_code=409,
                detail="Only active subscriptions can receive delivery state.",
            )
        sub.delivery_attempts += 1
        sub.last_delivery_attempt_at = now
        if req.delivered:
            sub.last_sent_at = now
            sub.last_digest_fingerprint = req.digest_fingerprint
            sub.last_delivery_error = None
        else:
            sub.last_delivery_error = (req.error or "delivery failed")[:500]
        session.commit()
        session.refresh(sub)
        return _subscription_response(sub)


def _subscription_response(sub: SignalSubscription) -> SignalSubscriptionResponse:
    return SignalSubscriptionResponse(
        id=str(sub.id),
        discord_user_id=sub.discord_user_id,
        discord_channel_id=sub.discord_channel_id,
        hours=sub.hours,
        limit=sub.limit,
        threshold_z=sub.threshold_z,
        interval_minutes=sub.interval_minutes,
        quiet_start_hour=sub.quiet_start_hour,
        quiet_end_hour=sub.quiet_end_hour,
        timezone_offset_minutes=sub.timezone_offset_minutes,
        status=sub.status,  # type: ignore[arg-type]
        created_at=sub.created_at,
        last_checked_at=sub.last_checked_at,
        last_sent_at=sub.last_sent_at,
        last_delivery_attempt_at=sub.last_delivery_attempt_at,
        delivery_attempts=sub.delivery_attempts,
        last_delivery_error=sub.last_delivery_error,
        last_digest_fingerprint=sub.last_digest_fingerprint,
    )


def _is_due(sub: SignalSubscription, now: datetime) -> bool:
    if sub.last_checked_at is None:
        return True
    return now >= sub.last_checked_at + timedelta(minutes=sub.interval_minutes)


def _is_quiet_now(sub: SignalSubscription, now: datetime) -> bool:
    if sub.quiet_start_hour is None or sub.quiet_end_hour is None:
        return False
    local_hour = (
        now + timedelta(minutes=sub.timezone_offset_minutes)
    ).hour
    start = sub.quiet_start_hour
    end = sub.quiet_end_hour
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def _digest_fingerprint(digest: dict[str, Any]) -> str:
    rows = [
        {
            "signal_type": row["signal_type"],
            "slug": row["slug"],
            "computed_at": row["computed_at"],
            "z_score": row["z_score"],
        }
        for row in digest.get("signals") or []
    ]
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def signal_subscription_max_active_per_user() -> int:
    return int(
        os.environ.get(
            "SIGNAL_SUBSCRIPTION_MAX_ACTIVE_PER_USER",
            DEFAULT_SIGNAL_SUBSCRIPTION_MAX_ACTIVE_PER_USER,
        )
    )
