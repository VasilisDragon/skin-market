"""Insights endpoints.

Three routes:

- ``GET /items/{slug}/insights`` — per-item: latest of each
  (insight_type, sub-key). Excludes ``daily_narrative`` (which is
  global, ADR 014 §5).
- ``GET /insights/narrative/latest`` — the latest daily narrative.
  Item-agnostic; Phase 7a addition for the bot's "what happened
  today" tool.
- ``GET /insights/anomalies/recent`` — currently-firing cross-source
  divergences + volume anomalies, joined with item metadata so the
  bot can render "what's interesting today" without a second
  round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.schemas import (
    AnomaliesResponse,
    AnomalyRow,
    InsightRow,
    InsightsResponse,
    NarrativeResponse,
    SignalDigestResponse,
    SignalDigestRow,
)
from api.watchlist_tiers import get_tier
from db.connection import get_engine

router = APIRouter(tags=["insights"])

ANOMALIES_DEFAULT_HOURS = 6
ANOMALIES_MAX_HOURS = 24
SIGNAL_DIGEST_DEFAULT_LIMIT = 8
SIGNAL_DIGEST_MAX_LIMIT = 20


@router.get(
    "/items/{slug}/insights",
    response_model=InsightsResponse,
)
def get_insights(slug: str) -> InsightsResponse:
    engine = get_engine()
    with Session(engine) as session:
        item_row = session.execute(
            text(
                "SELECT id, market_hash_name FROM items "
                "WHERE slug = :slug"
            ),
            {"slug": slug},
        ).first()
        if item_row is None:
            raise HTTPException(
                status_code=404, detail=f"Item not found: {slug!r}"
            )
        item_id = item_row.id
        market_hash_name = item_row.market_hash_name

        rows = session.execute(
            text(
                """
                SELECT DISTINCT ON (insight_type, meta_signature)
                    insight_type, computed_at, value, text_value, meta_info
                FROM (
                    SELECT
                        insight_type,
                        computed_at,
                        value,
                        text_value,
                        meta_info,
                        COALESCE(
                            meta_info->>'source_id',
                            CONCAT(
                                meta_info->>'source_a_id',
                                '/',
                                meta_info->>'source_b_id'
                            ),
                            ''
                        ) AS meta_signature
                    FROM insights
                    WHERE item_id = :item_id
                      AND insight_type != 'daily_narrative'
                ) AS sub
                ORDER BY insight_type, meta_signature, computed_at DESC
                """
            ),
            {"item_id": item_id},
        ).mappings().all()

    return InsightsResponse(
        slug=slug,
        tier=get_tier(market_hash_name),
        insights=[
            InsightRow(
                insight_type=row["insight_type"],
                computed_at=row["computed_at"],
                value=row["value"],
                text_value=row["text_value"],
                meta=dict(row["meta_info"] or {}),
            )
            for row in rows
        ],
    )


@router.get(
    "/insights/narrative/latest",
    response_model=NarrativeResponse,
)
def get_latest_narrative() -> NarrativeResponse:
    """Return the most recent ``daily_narrative`` insight row.

    404 if no narrative has been generated yet — the analytics
    scheduler runs the narrative job at 02:00 UTC daily; on a fresh
    deploy this will be empty until the first nightly cycle fires.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.execute(
                text(
                    """
                    SELECT computed_at, text_value, meta_info
                    FROM insights
                    WHERE insight_type = 'daily_narrative'
                    ORDER BY computed_at DESC
                    LIMIT 1
                    """
                )
            )
            .mappings()
            .first()
        )
    if row is None or not row.get("text_value"):
        raise HTTPException(
            status_code=404,
            detail=(
                "No daily narrative generated yet. The analytics "
                "scheduler runs the narrative job at 02:00 UTC; on a "
                "fresh deploy, wait for the first cycle."
            ),
        )
    return NarrativeResponse(
        computed_at=row["computed_at"],
        text=row["text_value"],
        meta=dict(row["meta_info"] or {}),
    )


@router.get(
    "/insights/anomalies/recent",
    response_model=AnomaliesResponse,
)
def get_recent_anomalies(
    hours: Annotated[
        int,
        Query(
            ge=1,
            le=ANOMALIES_MAX_HOURS,
            description=(
                f"Lookback window in hours. Default "
                f"{ANOMALIES_DEFAULT_HOURS}, max {ANOMALIES_MAX_HOURS}."
            ),
        ),
    ] = ANOMALIES_DEFAULT_HOURS,
) -> AnomaliesResponse:
    """Return cross-source divergences + volume anomalies from the last
    N hours, joined with item slug + display_name. Z-scores are signed.

    Sorted by ``computed_at`` DESC so the most-recent anomalies surface
    first. The bot's "what's interesting today" tool reads this
    directly without per-item lookups.
    """
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)

    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.execute(
                text(
                    """
                    SELECT
                        ins.insight_type,
                        i.slug,
                        i.display_name,
                        ins.computed_at,
                        ins.value AS z_score,
                        ins.meta_info
                    FROM insights ins
                    JOIN items i ON i.id = ins.item_id
                    WHERE ins.insight_type IN (
                        'cross_source_divergence',
                        'volume_anomaly'
                    )
                      AND ins.computed_at >= :since
                    ORDER BY ins.computed_at DESC, i.market_hash_name
                    """
                ),
                {"since": since},
            )
            .mappings()
            .all()
        )

    return AnomaliesResponse(
        since=since,
        count=len(rows),
        anomalies=[
            AnomalyRow(
                insight_type=row["insight_type"],
                slug=row["slug"],
                display_name=row["display_name"],
                computed_at=row["computed_at"],
                z_score=row["z_score"],
                meta=dict(row["meta_info"] or {}),
            )
            for row in rows
        ],
    )


@router.get(
    "/insights/signals/digest",
    response_model=SignalDigestResponse,
)
def get_signal_digest(
    hours: Annotated[
        int,
        Query(
            ge=1,
            le=ANOMALIES_MAX_HOURS,
            description=(
                f"Lookback window in hours. Default "
                f"{ANOMALIES_DEFAULT_HOURS}, max {ANOMALIES_MAX_HOURS}."
            ),
        ),
    ] = ANOMALIES_DEFAULT_HOURS,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=SIGNAL_DIGEST_MAX_LIMIT,
            description=(
                "Maximum ranked signals to return. Default "
                f"{SIGNAL_DIGEST_DEFAULT_LIMIT}, max {SIGNAL_DIGEST_MAX_LIMIT}."
            ),
        ),
    ] = SIGNAL_DIGEST_DEFAULT_LIMIT,
) -> SignalDigestResponse:
    """Return a compact ranked signal digest for Discord rendering."""
    generated_at = datetime.now(UTC)
    since = generated_at - timedelta(hours=hours)

    engine = get_engine()
    with Session(engine) as session:
        total = session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM insights
                WHERE insight_type IN (
                    'cross_source_divergence',
                    'volume_anomaly'
                )
                  AND computed_at >= :since
                """
            ),
            {"since": since},
        ).scalar_one()
        rows = (
            session.execute(
                text(
                    """
                    SELECT
                        ins.insight_type,
                        i.slug,
                        i.display_name,
                        ins.computed_at,
                        ins.value AS z_score,
                        ins.meta_info
                    FROM insights ins
                    JOIN items i ON i.id = ins.item_id
                    WHERE ins.insight_type IN (
                        'cross_source_divergence',
                        'volume_anomaly'
                    )
                      AND ins.computed_at >= :since
                    ORDER BY ABS(ins.value) DESC, ins.computed_at DESC
                    LIMIT :limit
                    """
                ),
                {"since": since, "limit": limit},
            )
            .mappings()
            .all()
        )

    return SignalDigestResponse(
        generated_at=generated_at,
        since=since,
        hours=hours,
        total_anomalies=total,
        returned_count=len(rows),
        signals=[
            SignalDigestRow(
                signal_type=row["insight_type"],
                slug=row["slug"],
                display_name=row["display_name"],
                computed_at=row["computed_at"],
                z_score=row["z_score"],
                severity=_signal_severity(row["z_score"]),
                summary=_signal_summary(row["insight_type"], row["meta_info"] or {}),
                meta=dict(row["meta_info"] or {}),
            )
            for row in rows
        ],
    )


def _signal_severity(z_score) -> str:
    z_abs = abs(float(z_score or 0))
    if z_abs >= 4:
        return "extreme"
    if z_abs >= 3:
        return "high"
    return "moderate"


def _signal_summary(insight_type: str, meta: dict) -> str:
    if insight_type == "volume_anomaly":
        observed = meta.get("observed_volume")
        baseline = meta.get("baseline_mean")
        source = meta.get("source_name") or meta.get("source_id") or "source"
        if observed is not None and baseline is not None:
            return (
                f"Volume on {source} is outside its baseline "
                f"({observed} observed vs {baseline} average)."
            )
        return f"Volume on {source} is outside its recent baseline."

    observed = meta.get("observed_spread")
    baseline = meta.get("baseline_mean")
    source_a = meta.get("source_a_name") or meta.get("source_a_id") or "source A"
    source_b = meta.get("source_b_name") or meta.get("source_b_id") or "source B"
    if observed is not None and baseline is not None:
        return (
            f"Spread between {source_a} and {source_b} is unusual "
            f"({observed} observed vs {baseline} average)."
        )
    return f"Spread between {source_a} and {source_b} is unusual."
