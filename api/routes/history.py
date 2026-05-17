"""``/items/{slug}/history`` — bounded time-series for one item.

Defaults applied when query params are absent:

- ``since`` = now - 7 days (``HISTORY_DEFAULT_DAYS``)
- ``limit`` = 500 rows (``HISTORY_DEFAULT_LIMIT``)
- ``limit`` is hard-capped at 5000 (``HISTORY_MAX_LIMIT``) to keep a
  pathological "give me all of it" query from returning a 10MB JSON.

Both defaults are documented in the OpenAPI example (see schemas) so
callers reading ``/docs`` know what they get if they pass nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.schemas import HistoryObservation, HistoryResponse
from api.watchlist_tiers import get_tier
from db.connection import get_engine

router = APIRouter(tags=["history"])

HISTORY_DEFAULT_DAYS = 7
HISTORY_DEFAULT_LIMIT = 500
HISTORY_MAX_LIMIT = 5000


@router.get(
    "/items/{slug}/history",
    response_model=HistoryResponse,
)
def get_history(
    slug: str,
    source: Annotated[
        str | None,
        Query(
            description="Filter to one source name (e.g. 'steam_market').",
        ),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(
            description=(
                f"Lower bound (inclusive). ISO 8601 datetime. "
                f"Default: now - {HISTORY_DEFAULT_DAYS} days."
            ),
        ),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="Upper bound (inclusive). Default: now."),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=HISTORY_MAX_LIMIT,
            description=(
                f"Maximum rows. Default {HISTORY_DEFAULT_LIMIT}, max "
                f"{HISTORY_MAX_LIMIT}."
            ),
        ),
    ] = HISTORY_DEFAULT_LIMIT,
) -> HistoryResponse:
    now = datetime.now(UTC)
    effective_since = since or (now - timedelta(days=HISTORY_DEFAULT_DAYS))
    effective_until = until or now

    if effective_since > effective_until:
        raise HTTPException(
            status_code=400,
            detail="`since` must be earlier than `until`.",
        )

    engine = get_engine()
    with Session(engine) as session:
        # Existence check up front — we need market_hash_name for the
        # tier lookup regardless of whether the time-series is empty.
        # Cheaper than running the time-series query and then
        # double-checking on the empty path, and uniform with the
        # other routes (Step 8: routes query items first).
        item_row = session.execute(
            text(
                "SELECT market_hash_name FROM items WHERE slug = :slug"
            ),
            {"slug": slug},
        ).first()
        if item_row is None:
            raise HTTPException(
                status_code=404, detail=f"Item not found: {slug!r}"
            )
        market_hash_name = item_row.market_hash_name

        rows = session.execute(
            text(
                """
                SELECT
                    p.timestamp,
                    s.name AS source_name,
                    s.denomination,
                    p.price,
                    p.volume
                FROM prices p
                JOIN sources s ON s.id = p.source_id
                JOIN items i  ON i.id = p.item_id
                WHERE i.slug = :slug
                  AND s.enabled = TRUE
                  AND p.timestamp >= :since
                  AND p.timestamp <= :until
                  AND (CAST(:source AS TEXT) IS NULL OR s.name = :source)
                ORDER BY p.timestamp DESC
                LIMIT :limit
                """
            ),
            {
                "slug": slug,
                "since": effective_since,
                "until": effective_until,
                "source": source,
                "limit": limit,
            },
        ).mappings().all()

    return HistoryResponse(
        slug=slug,
        tier=get_tier(market_hash_name),
        source=source,
        since=effective_since,
        until=effective_until,
        limit=limit,
        count=len(rows),
        observations=[
            HistoryObservation(
                timestamp=row["timestamp"],
                source=row["source_name"],
                denomination=row["denomination"],
                price=row["price"],
                volume=row["volume"],
            )
            for row in rows
        ],
    )
