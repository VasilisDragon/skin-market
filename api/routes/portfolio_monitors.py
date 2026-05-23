"""Recurring Discord portfolio baseline monitors."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.entitlements import effective_entitlement_policy
from api.routes.portfolio import create_portfolio_snapshot
from api.schemas import (
    PortfolioMonitorCancelRequest,
    PortfolioMonitorCreateRequest,
    PortfolioMonitorDeliveryRequest,
    PortfolioMonitorEvaluateRequest,
    PortfolioMonitorEvaluateResponse,
    PortfolioMonitorResponse,
    PortfolioSnapshotCreateRequest,
)
from db.connection import get_engine
from db.models import PortfolioMonitor

router = APIRouter(tags=["portfolio monitors"])

DEFAULT_PORTFOLIO_MONITOR_MAX_ACTIVE_PER_USER = 1


@router.post("/portfolio/monitors", response_model=PortfolioMonitorResponse)
def create_portfolio_monitor(
    req: PortfolioMonitorCreateRequest,
) -> PortfolioMonitorResponse:
    engine = get_engine()
    with Session(engine) as session:
        active_count = session.scalar(
            select(func.count())
            .select_from(PortfolioMonitor)
            .where(
                PortfolioMonitor.discord_user_id == req.discord_user_id,
                PortfolioMonitor.status == "active",
            )
        )
        policy = effective_entitlement_policy(
            session,
            req.discord_user_id,
            default_active_price_alerts=25,
            default_portfolio_snapshots_per_day=10,
            default_signal_subscriptions=1,
            default_portfolio_monitors=portfolio_monitor_max_active_per_user(),
        )
        max_active = policy.portfolio_monitors
        if active_count is not None and active_count >= max_active:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Active portfolio monitor quota reached "
                    f"({active_count}/{max_active})."
                ),
            )
        monitor = PortfolioMonitor(
            discord_user_id=req.discord_user_id,
            discord_channel_id=req.discord_channel_id,
            inventory_url=req.inventory_url,
            interval_minutes=req.interval_minutes,
            change_threshold_pct=req.change_threshold_pct,
            quiet_start_hour=req.quiet_start_hour,
            quiet_end_hour=req.quiet_end_hour,
            timezone_offset_minutes=req.timezone_offset_minutes,
            status="active",
        )
        session.add(monitor)
        session.commit()
        session.refresh(monitor)
        return _monitor_response(monitor)


@router.get("/portfolio/monitors", response_model=list[PortfolioMonitorResponse])
def list_portfolio_monitors(
    discord_user_id: str = Query(...),
    include_inactive: bool = False,
) -> list[PortfolioMonitorResponse]:
    stmt = (
        select(PortfolioMonitor)
        .where(PortfolioMonitor.discord_user_id == discord_user_id)
        .order_by(PortfolioMonitor.created_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(PortfolioMonitor.status == "active")

    engine = get_engine()
    with Session(engine) as session:
        rows = session.execute(stmt).scalars().all()
    return [_monitor_response(row) for row in rows]


@router.post(
    "/portfolio/monitors/{monitor_id}/cancel",
    response_model=PortfolioMonitorResponse,
)
def cancel_portfolio_monitor(
    monitor_id: str,
    req: PortfolioMonitorCancelRequest,
) -> PortfolioMonitorResponse:
    try:
        parsed_id = uuid.UUID(monitor_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Monitor not found") from exc

    engine = get_engine()
    with Session(engine) as session:
        monitor = session.execute(
            select(PortfolioMonitor).where(
                PortfolioMonitor.id == parsed_id,
                PortfolioMonitor.discord_user_id == req.discord_user_id,
            )
        ).scalar_one_or_none()
        if monitor is None:
            raise HTTPException(status_code=404, detail="Monitor not found")
        if monitor.status == "active":
            monitor.status = "cancelled"
            session.commit()
            session.refresh(monitor)
        return _monitor_response(monitor)


@router.post(
    "/portfolio/monitors/evaluate",
    response_model=PortfolioMonitorEvaluateResponse,
)
def evaluate_portfolio_monitors(
    req: PortfolioMonitorEvaluateRequest,
) -> PortfolioMonitorEvaluateResponse:
    now = datetime.now(UTC)
    checked_count = 0
    due = []

    engine = get_engine()
    with Session(engine) as session:
        monitors = session.execute(
            select(PortfolioMonitor)
            .where(PortfolioMonitor.status == "active")
            .order_by(PortfolioMonitor.created_at)
            .limit(req.limit)
        ).scalars().all()

        for monitor in monitors:
            if _is_quiet_now(monitor, now):
                continue
            if not _is_due(monitor, now):
                continue
            checked_count += 1
            monitor.last_checked_at = now
            snapshot_result = create_portfolio_snapshot(
                PortfolioSnapshotCreateRequest(
                    discord_user_id=monitor.discord_user_id,
                    inventory_url=monitor.inventory_url,
                )
            )
            if snapshot_result.snapshot is None:
                continue
            event_type = _monitor_event_type(monitor, snapshot_result)
            if event_type is None:
                continue
            due.append(
                {
                    "monitor": _monitor_response(monitor),
                    "snapshot_result": snapshot_result.model_dump(mode="json"),
                    "event_type": event_type,
                }
            )

        session.commit()

    return PortfolioMonitorEvaluateResponse(checked_count=checked_count, due=due)


@router.post(
    "/portfolio/monitors/{monitor_id}/delivery",
    response_model=PortfolioMonitorResponse,
)
def record_portfolio_monitor_delivery(
    monitor_id: str,
    req: PortfolioMonitorDeliveryRequest,
) -> PortfolioMonitorResponse:
    try:
        parsed_id = uuid.UUID(monitor_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Monitor not found") from exc

    snapshot_uuid = None
    if req.snapshot_id is not None:
        try:
            snapshot_uuid = uuid.UUID(req.snapshot_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid snapshot id") from exc

    now = datetime.now(UTC)
    engine = get_engine()
    with Session(engine) as session:
        monitor = session.execute(
            select(PortfolioMonitor).where(PortfolioMonitor.id == parsed_id)
        ).scalar_one_or_none()
        if monitor is None:
            raise HTTPException(status_code=404, detail="Monitor not found")
        if monitor.status != "active":
            raise HTTPException(
                status_code=409,
                detail="Only active monitors can receive delivery state.",
            )
        monitor.delivery_attempts += 1
        monitor.last_delivery_attempt_at = now
        if req.delivered:
            monitor.last_sent_at = now
            monitor.last_snapshot_id = snapshot_uuid
            monitor.last_delivery_error = None
        else:
            monitor.last_delivery_error = (req.error or "delivery failed")[:500]
        session.commit()
        session.refresh(monitor)
        return _monitor_response(monitor)


def _monitor_event_type(
    monitor: PortfolioMonitor,
    snapshot_result,
) -> str | None:
    if monitor.last_snapshot_id is None:
        return "initial_snapshot"
    delta = snapshot_result.delta_vs_previous
    if not delta or delta.get("mid_change_pct") is None:
        return None
    pct = Decimal(str(delta["mid_change_pct"]))
    if abs(pct) >= monitor.change_threshold_pct:
        return "threshold_crossed"
    return None


def _monitor_response(monitor: PortfolioMonitor) -> PortfolioMonitorResponse:
    return PortfolioMonitorResponse(
        id=str(monitor.id),
        discord_user_id=monitor.discord_user_id,
        discord_channel_id=monitor.discord_channel_id,
        inventory_url=monitor.inventory_url,
        interval_minutes=monitor.interval_minutes,
        change_threshold_pct=monitor.change_threshold_pct,
        quiet_start_hour=monitor.quiet_start_hour,
        quiet_end_hour=monitor.quiet_end_hour,
        timezone_offset_minutes=monitor.timezone_offset_minutes,
        status=monitor.status,  # type: ignore[arg-type]
        created_at=monitor.created_at,
        last_checked_at=monitor.last_checked_at,
        last_sent_at=monitor.last_sent_at,
        last_snapshot_id=(
            str(monitor.last_snapshot_id)
            if monitor.last_snapshot_id is not None
            else None
        ),
        last_delivery_attempt_at=monitor.last_delivery_attempt_at,
        delivery_attempts=monitor.delivery_attempts,
        last_delivery_error=monitor.last_delivery_error,
    )


def _is_due(monitor: PortfolioMonitor, now: datetime) -> bool:
    if monitor.last_checked_at is None:
        return True
    return now >= monitor.last_checked_at + timedelta(minutes=monitor.interval_minutes)


def _is_quiet_now(monitor: PortfolioMonitor, now: datetime) -> bool:
    if monitor.quiet_start_hour is None or monitor.quiet_end_hour is None:
        return False
    local_hour = (
        now + timedelta(minutes=monitor.timezone_offset_minutes)
    ).hour
    start = monitor.quiet_start_hour
    end = monitor.quiet_end_hour
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def portfolio_monitor_max_active_per_user() -> int:
    return int(
        os.environ.get(
            "PORTFOLIO_MONITOR_MAX_ACTIVE_PER_USER",
            DEFAULT_PORTFOLIO_MONITOR_MAX_ACTIVE_PER_USER,
        )
    )
