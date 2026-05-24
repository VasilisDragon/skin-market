"""``GET /items/{slug}/drift`` — most-recent drift verdict per pair
for one item.

The drift detector (analytics/drift.py, ADR 022) writes
``insight_type='drift_verdict'`` rows to the ``insights`` table on a
30-minute cycle, one per meaningful pair (skinport↔pricempire_skinport,
dmarket↔pricempire_dmarket per ADR 018). This endpoint surfaces the
latest row for each pair so the bot can present a focused
"how does our curated data compare to Pricempire's?" view without
parsing the heterogeneous /items/{slug}/insights stream.

Query strategy

``DISTINCT ON (meta_info->>'source_a_id', meta_info->>'source_b_id')``
gives the latest row per pair. The keys match the detector's
``_build_meta_info`` output (analytics/drift.py:578-581). The pattern
mirrors the per-item insights endpoint (api/routes/insights.py) where
``meta_signature`` collapses to the same source-id tuple for drift
rows.

Status-code contract

- 404 when the slug is not in the items table.
- 200 with ``tier="curated"``, ``pairs=[]`` when the detector hasn't
  produced rows yet (fresh deploy, classifier-disabled period).
- 200 with ``tier="curated"``, ``pairs=[…1 entry…]`` for items where
  only one pair has produced a row while one side is still warming up.
- 200 with ``tier="curated"``, ``pairs=[…2 entries…]`` for steady-
  state curated-tier items.
- 200 with ``tier="featured"`` or ``tier="substrate"``, ``pairs=[]``
  — drift detection skips both tier classes by construction.

Why not 422 for featured/substrate
The caller asked a sensible question about a known item; the empty
answer is structural, not a caller error. Matches the precedent of
``/deals/evaluate`` returning 200 with ``verdict="no_comparable_data"``
rather than 422.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.schemas import DriftPairVerdict, DriftResponse
from api.watchlist_tiers import get_tier
from db.connection import get_engine

router = APIRouter(tags=["drift"])


@router.get("/items/{slug}/drift", response_model=DriftResponse)
def get_drift(slug: str) -> DriftResponse:
    engine = get_engine()
    with Session(engine) as session:
        item = session.execute(
            text(
                "SELECT id, display_name, market_hash_name "
                "FROM items WHERE slug = :slug"
            ),
            {"slug": slug},
        ).mappings().first()
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"Item not found: {slug!r}"
            )

        # DISTINCT ON (source_a_id, source_b_id) ORDER BY ... computed_at DESC
        # → latest row per pair. The source_a_id / source_b_id keys are
        # written by analytics/drift.py::_build_meta_info.
        rows = session.execute(
            text(
                """
                SELECT DISTINCT ON (
                    meta_info->>'source_a_id',
                    meta_info->>'source_b_id'
                )
                    computed_at,
                    value AS drift,
                    meta_info
                FROM insights
                WHERE item_id = :item_id
                  AND insight_type = 'drift_verdict'
                ORDER BY
                    meta_info->>'source_a_id',
                    meta_info->>'source_b_id',
                    computed_at DESC
                """
            ),
            {"item_id": item["id"]},
        ).mappings().all()

    pairs = [_row_to_pair_verdict(row) for row in rows]

    return DriftResponse(
        slug=slug,
        display_name=item["display_name"],
        tier=get_tier(item["market_hash_name"]),
        pairs=pairs,
    )


def _row_to_pair_verdict(row) -> DriftPairVerdict:
    """Map one DB row → DriftPairVerdict.

    ``meta_info`` JSONB carries the detector's frozen-dataclass output
    rendered as strings (MoneyStr discipline — analytics/drift.py
    ``_build_meta_info``). We round-trip through Decimal for drift and
    threshold so MoneyStr's PlainSerializer doesn't try to re-stringify
    an already-string value.
    """
    meta = dict(row["meta_info"] or {})

    drift_value = row["drift"]
    if drift_value is not None and not isinstance(drift_value, Decimal):
        drift_value = Decimal(str(drift_value))

    threshold_str = meta.get("threshold_used")
    threshold = (
        Decimal(threshold_str)
        if threshold_str is not None
        else Decimal("0")
    )

    curated_price = _maybe_decimal(meta.get("curated_price"))
    pricempire_price = _maybe_decimal(meta.get("pricempire_price"))

    return DriftPairVerdict(
        source_a=meta.get("source_a_name", ""),
        source_b=meta.get("source_b_name", ""),
        verdict=meta.get("verdict"),
        drift=drift_value,
        threshold_used=threshold,
        classification=meta.get("classification"),
        threshold_multiplier=float(meta.get("threshold_multiplier", 1.0)),
        computed_at=row["computed_at"],
        curated_price=curated_price,
        pricempire_price=pricempire_price,
        curated_last_polled_at=_maybe_iso(meta.get("curated_last_polled_at")),
        pricempire_last_polled_at=_maybe_iso(
            meta.get("pricempire_last_polled_at")
        ),
        curated_age_min=meta.get("curated_age_min"),
        pricempire_age_min=meta.get("pricempire_age_min"),
        note=meta.get("note"),
    )


def _maybe_decimal(value):
    """``None`` passes through; strings get parsed to Decimal."""
    if value is None:
        return None
    return Decimal(str(value))


def _maybe_iso(value):
    """ISO-8601 string → datetime; ``None`` passes through.

    Pydantic v2 parses ISO strings into datetimes when the field type
    is ``datetime``, so passing the string directly also works — but
    being explicit keeps the route's intent obvious.
    """
    if value is None:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value)
