"""``/items/{slug}/insights`` — latest of each per-item insight.

Returns one row per ``(insight_type, sub-key)`` where ``sub-key`` is
``meta_info->>'source_id'`` for per-source insights (moving averages),
``source_a_id/source_b_id`` for per-pair insights (cross_source_spread),
or empty for item-level insights (cross_source_view, volume_anomaly,
cross_source_divergence).

``daily_narrative`` is excluded — it's a global insight pinned to an
arbitrary "first item" by the analytics layer (see
``analytics/narrative.py``), so surfacing it via a per-item endpoint
would either lie (it's not about THIS item) or be inconsistent (it
only shows up under one slug). The bot fetches the daily narrative
via a different path in Phase 7. ADR 014 §5.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.schemas import InsightRow, InsightsResponse
from db.connection import get_engine

router = APIRouter(tags=["insights"])


@router.get(
    "/items/{slug}/insights",
    response_model=InsightsResponse,
)
def get_insights(slug: str) -> InsightsResponse:
    engine = get_engine()
    with Session(engine) as session:
        item_id = session.execute(
            text("SELECT id FROM items WHERE slug = :slug"),
            {"slug": slug},
        ).scalar_one_or_none()
        if item_id is None:
            raise HTTPException(
                status_code=404, detail=f"Item not found: {slug!r}"
            )

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
