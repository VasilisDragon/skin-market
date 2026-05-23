"""Persisted Discord portfolio snapshots."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.routes.asset_valuation import inventory_portfolio_market_baseline
from api.schemas import (
    InventorySummaryRequest,
    PortfolioSnapshotCreateRequest,
    PortfolioSnapshotCreateResponse,
    PortfolioSnapshotResponse,
    PortfolioSnapshotTrendResponse,
)
from db.connection import get_engine
from db.models import PortfolioSnapshot

router = APIRouter(tags=["portfolio snapshots"])

_CENTS = Decimal("0.01")
_PCT = Decimal("0.01")


@router.post(
    "/portfolio/snapshots",
    response_model=PortfolioSnapshotCreateResponse,
)
def create_portfolio_snapshot(
    req: PortfolioSnapshotCreateRequest,
) -> PortfolioSnapshotCreateResponse:
    """Read a public inventory baseline and persist a summary-level snapshot."""
    summary = inventory_portfolio_market_baseline(
        InventorySummaryRequest(inventory_url=req.inventory_url)
    )
    if summary.reference is None:
        return PortfolioSnapshotCreateResponse(
            status=summary.status,
            message=summary.message,
            snapshot=None,
            delta_vs_previous=None,
            summary=summary,
        )

    summary_data = summary.model_dump(mode="json")
    baseline = summary_data["portfolio_baseline"]
    steam_id = str(summary_data["reference"]["steam_id"])

    engine = get_engine()
    with Session(engine) as session:
        previous = session.execute(
            select(PortfolioSnapshot)
            .where(
                PortfolioSnapshot.discord_user_id == req.discord_user_id,
                PortfolioSnapshot.steam_id == steam_id,
            )
            .order_by(PortfolioSnapshot.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        snapshot = PortfolioSnapshot(
            discord_user_id=req.discord_user_id,
            steam_id=steam_id,
            inventory_url=req.inventory_url,
            status=summary.status,
            reason=summary.reason,
            message=summary.message,
            currency=baseline["currency"] if baseline else None,
            baseline_low=_decimal_from_baseline(baseline, "low"),
            baseline_mid=_decimal_from_baseline(baseline, "mid"),
            baseline_high=_decimal_from_baseline(baseline, "high"),
            priced_count=_int_from_baseline(baseline, "priced_count"),
            unpriced_count=_int_from_baseline(baseline, "unpriced_count"),
            stickered_count=_int_from_baseline(baseline, "stickered_count"),
            top_item_share_pct=_decimal_from_baseline(
                baseline,
                "top_item_share_pct",
            ),
            portfolio_baseline=baseline,
            top_items=summary_data["top_items"],
            largest_spread_items=summary_data["largest_spread_items"],
            unpriced_sample=summary_data["unpriced_sample"],
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        delta = _snapshot_delta(snapshot, previous)
        response = _snapshot_response(snapshot)

    return PortfolioSnapshotCreateResponse(
        status=summary.status,
        message=summary.message,
        snapshot=response,
        delta_vs_previous=delta,
        summary=summary,
    )


@router.get(
    "/portfolio/snapshots",
    response_model=list[PortfolioSnapshotResponse],
)
def list_portfolio_snapshots(
    discord_user_id: str = Query(...),
    steam_id: str | None = None,
    limit: int = Query(default=10, ge=1, le=100),
) -> list[PortfolioSnapshotResponse]:
    stmt = (
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.discord_user_id == discord_user_id)
        .order_by(PortfolioSnapshot.created_at.desc())
        .limit(limit)
    )
    if steam_id is not None:
        stmt = stmt.where(PortfolioSnapshot.steam_id == steam_id)

    engine = get_engine()
    with Session(engine) as session:
        rows = session.execute(stmt).scalars().all()
    return [_snapshot_response(row) for row in rows]


@router.get(
    "/portfolio/snapshots/trend",
    response_model=PortfolioSnapshotTrendResponse,
)
def portfolio_snapshot_trend(
    discord_user_id: str = Query(...),
    steam_id: str | None = None,
    limit: int = Query(default=30, ge=2, le=100),
) -> PortfolioSnapshotTrendResponse:
    engine = get_engine()
    with Session(engine) as session:
        resolved_steam_id = steam_id
        if resolved_steam_id is None:
            latest = session.execute(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.discord_user_id == discord_user_id)
                .order_by(PortfolioSnapshot.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            resolved_steam_id = latest.steam_id if latest is not None else None

        rows: list[PortfolioSnapshot] = []
        if resolved_steam_id is not None:
            rows = session.execute(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.discord_user_id == discord_user_id,
                    PortfolioSnapshot.steam_id == resolved_steam_id,
                )
                .order_by(PortfolioSnapshot.created_at.desc())
                .limit(limit)
            ).scalars().all()

        latest_row = rows[0] if rows else None
        previous_row = rows[1] if len(rows) > 1 else None
        oldest_row = rows[-1] if len(rows) > 1 else None
        snapshots = [_snapshot_response(row) for row in rows]

    return PortfolioSnapshotTrendResponse(
        discord_user_id=discord_user_id,
        steam_id=resolved_steam_id,
        count=len(rows),
        latest=_snapshot_response(latest_row) if latest_row is not None else None,
        previous=(
            _snapshot_response(previous_row) if previous_row is not None else None
        ),
        delta_vs_previous=_snapshot_delta(latest_row, previous_row),
        delta_since_oldest=_snapshot_delta(latest_row, oldest_row),
        snapshots=snapshots,
    )


def _decimal_from_baseline(
    baseline: dict[str, Any] | None,
    key: str,
) -> Decimal | None:
    if baseline is None or baseline.get(key) is None:
        return None
    return Decimal(str(baseline[key]))


def _int_from_baseline(baseline: dict[str, Any] | None, key: str) -> int:
    if baseline is None or baseline.get(key) is None:
        return 0
    return int(baseline[key])


def _snapshot_delta(
    latest: PortfolioSnapshot | None,
    previous: PortfolioSnapshot | None,
) -> dict[str, Any] | None:
    if latest is None or previous is None:
        return None
    mid_change = None
    mid_change_pct = None
    if latest.baseline_mid is not None and previous.baseline_mid is not None:
        mid_change_value = latest.baseline_mid - previous.baseline_mid
        mid_change = _money(mid_change_value)
        if previous.baseline_mid > 0:
            mid_change_pct = _pct(mid_change_value / previous.baseline_mid * 100)

    return {
        "from_snapshot_id": str(previous.id),
        "to_snapshot_id": str(latest.id),
        "from_created_at": previous.created_at.isoformat(),
        "to_created_at": latest.created_at.isoformat(),
        "mid_change": mid_change,
        "mid_change_pct": mid_change_pct,
        "priced_count_change": latest.priced_count - previous.priced_count,
        "unpriced_count_change": latest.unpriced_count - previous.unpriced_count,
        "stickered_count_change": latest.stickered_count - previous.stickered_count,
    }


def _snapshot_response(snapshot: PortfolioSnapshot) -> PortfolioSnapshotResponse:
    return PortfolioSnapshotResponse(
        id=str(snapshot.id),
        discord_user_id=snapshot.discord_user_id,
        steam_id=snapshot.steam_id,
        inventory_url=snapshot.inventory_url,
        status=snapshot.status,  # type: ignore[arg-type]
        reason=snapshot.reason,
        message=snapshot.message,
        created_at=snapshot.created_at,
        portfolio_baseline=snapshot.portfolio_baseline,
        top_items=snapshot.top_items,
        largest_spread_items=snapshot.largest_spread_items,
        unpriced_sample=snapshot.unpriced_sample,
    )


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _pct(value: Decimal) -> str:
    return str(value.quantize(_PCT, rounding=ROUND_HALF_UP))
