"""Persistent Discord-owned price alerts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from api.asset_valuation import PricePoint, load_latest_usd_price_points
from api.schemas import (
    PriceAlertCancelRequest,
    PriceAlertCreateRequest,
    PriceAlertEvaluateRequest,
    PriceAlertEvaluateResponse,
    PriceAlertResponse,
)
from db.connection import get_engine
from db.models import Item, PriceAlert

router = APIRouter(tags=["alerts"])


@router.post("/alerts/price", response_model=PriceAlertResponse)
def create_price_alert(req: PriceAlertCreateRequest) -> PriceAlertResponse:
    engine = get_engine()
    with Session(engine) as session:
        item = session.execute(
            select(Item).where(Item.slug == req.slug)
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail=f"Item not found: {req.slug!r}")

        alert = PriceAlert(
            discord_user_id=req.discord_user_id,
            discord_channel_id=req.discord_channel_id,
            item_id=item.id,
            slug_snapshot=item.slug,
            display_name_snapshot=item.display_name,
            currency=req.currency,
            direction=req.direction,
            threshold_price=req.threshold_price,
            status="active",
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)
        return _alert_response(alert)


@router.get("/alerts/price", response_model=list[PriceAlertResponse])
def list_price_alerts(
    discord_user_id: str = Query(...),
    include_inactive: bool = False,
) -> list[PriceAlertResponse]:
    stmt = (
        select(PriceAlert)
        .where(PriceAlert.discord_user_id == discord_user_id)
        .order_by(PriceAlert.created_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(PriceAlert.status == "active")

    engine = get_engine()
    with Session(engine) as session:
        rows = session.execute(stmt).scalars().all()
    return [_alert_response(row) for row in rows]


@router.post("/alerts/price/{alert_id}/cancel", response_model=PriceAlertResponse)
def cancel_price_alert(
    alert_id: str,
    req: PriceAlertCancelRequest,
) -> PriceAlertResponse:
    try:
        parsed_id = uuid.UUID(alert_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Alert not found") from exc

    engine = get_engine()
    with Session(engine) as session:
        alert = session.execute(
            select(PriceAlert).where(
                PriceAlert.id == parsed_id,
                PriceAlert.discord_user_id == req.discord_user_id,
            )
        ).scalar_one_or_none()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        if alert.status == "active":
            alert.status = "cancelled"
            session.commit()
            session.refresh(alert)
        return _alert_response(alert)


@router.post("/alerts/price/evaluate", response_model=PriceAlertEvaluateResponse)
def evaluate_price_alerts(
    req: PriceAlertEvaluateRequest,
) -> PriceAlertEvaluateResponse:
    """Evaluate active alerts and mark newly triggered rows.

    The route is intentionally API-side. The Discord bot can call this from a
    delivery loop without exposing alert math to the LLM.
    """
    engine = get_engine()
    now = datetime.now(UTC)
    triggered: list[PriceAlert] = []
    checked_count = 0

    with Session(engine) as session:
        alerts = session.execute(
            select(PriceAlert)
            .where(PriceAlert.status == "active")
            .order_by(PriceAlert.created_at)
            .limit(req.limit)
        ).scalars().all()

        for alert in alerts:
            checked_count += 1
            current = _current_alert_price(session, alert)
            alert.last_checked_at = now
            if current is None:
                continue
            if not _is_triggered(alert, current["price"]):
                continue
            alert.status = "triggered"
            alert.triggered_at = now
            alert.trigger_price = current["price"]
            alert.trigger_source = current["source"]
            triggered.append(alert)

        session.commit()
        for alert in triggered:
            session.refresh(alert)

    return PriceAlertEvaluateResponse(
        checked_count=checked_count,
        triggered=[_alert_response(row) for row in triggered],
    )


def _current_alert_price(
    session: Session,
    alert: PriceAlert,
) -> dict[str, Any] | None:
    item = session.execute(
        select(Item.market_hash_name).where(Item.id == alert.item_id)
    ).scalar_one_or_none()
    if item is None:
        return None

    if alert.currency == "usd":
        points = load_latest_usd_price_points(session, item)
    else:
        points = _latest_wallet_credit_points(session, alert.item_id)
    if not points:
        return None

    key = min if alert.direction == "at_or_below" else max
    selected = key(points, key=lambda point: point.price)
    return {"price": selected.price, "source": selected.source}


def _latest_wallet_credit_points(
    session: Session,
    item_id: uuid.UUID,
) -> list[PricePoint]:
    rows = session.execute(
        text(
            """
            SELECT
                s.name AS source,
                p.price,
                p.volume,
                ol.last_observed_at AS observed_at
            FROM observation_log ol
            JOIN sources s ON s.id = ol.source_id
            JOIN LATERAL (
                SELECT price, volume, timestamp
                FROM prices
                WHERE item_id = ol.item_id
                  AND source_id = ol.source_id
                ORDER BY timestamp DESC
                LIMIT 1
            ) p ON TRUE
            WHERE ol.item_id = :item_id
              AND s.enabled = TRUE
              AND s.denomination = 'wallet_credit'
            ORDER BY s.name
            """
        ),
        {"item_id": item_id},
    ).mappings()
    return [
        PricePoint(
            source=row["source"],
            source_family="direct",
            price=row["price"],
            volume=row["volume"],
            observed_at=(
                row["observed_at"].isoformat()
                if row["observed_at"] is not None
                else None
            ),
        )
        for row in rows
    ]


def _is_triggered(alert: PriceAlert, current_price: Decimal) -> bool:
    if alert.direction == "at_or_below":
        return current_price <= alert.threshold_price
    return current_price >= alert.threshold_price


def _alert_response(alert: PriceAlert) -> PriceAlertResponse:
    return PriceAlertResponse(
        id=str(alert.id),
        discord_user_id=alert.discord_user_id,
        discord_channel_id=alert.discord_channel_id,
        slug=alert.slug_snapshot,
        display_name=alert.display_name_snapshot,
        direction=alert.direction,  # type: ignore[arg-type]
        threshold_price=alert.threshold_price,
        currency=alert.currency,  # type: ignore[arg-type]
        status=alert.status,  # type: ignore[arg-type]
        created_at=alert.created_at,
        last_checked_at=alert.last_checked_at,
        triggered_at=alert.triggered_at,
        trigger_price=alert.trigger_price,
        trigger_source=alert.trigger_source,
    )
