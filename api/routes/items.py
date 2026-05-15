"""``/items`` family routes.

Endpoints:

- ``GET /items``               — list the full watchlist.
- ``GET /items/{slug}``        — single item's metadata.
- ``GET /items/{slug}/price``  — latest per-source price snapshot. The
  enforcement point for "no collapsed price field" — every row carries
  the source's name, ``denomination``, and two distinct freshness
  timestamps: ``last_polled_at`` (from ``observation_log``, the
  last-successful-poll signal) and ``last_changed_at`` (from
  ``prices.timestamp``, the last time the dedup gate let a row through).
  Splitting these is load-bearing: see ADR 017.

Source iteration: ``sources WHERE enabled = TRUE``. Adding a fourth
source remains a config change (row insert + collector subclass), not
a code change here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from api.schemas import (
    Item,
    ItemDetail,
    PerSourcePrice,
    PriceResponse,
)
from db.connection import get_engine
from db.models import Item as ItemModel

router = APIRouter(tags=["items"])


@router.get("/items", response_model=list[Item])
def list_items() -> list[Item]:
    """Return the full watchlist ordered by display name.

    No pagination — the watchlist is 48 items and the bot will fetch
    this once and cache locally. Revisit if/when watchlist grows past
    a few hundred.
    """
    engine = get_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(
                ItemModel.slug,
                ItemModel.market_hash_name,
                ItemModel.display_name,
            ).order_by(ItemModel.display_name)
        ).all()
    return [
        Item(
            slug=row.slug,
            market_hash_name=row.market_hash_name,
            display_name=row.display_name,
        )
        for row in rows
    ]


@router.get("/items/{slug}", response_model=ItemDetail)
def get_item(slug: str) -> ItemDetail:
    engine = get_engine()
    with Session(engine) as session:
        row = session.execute(
            select(ItemModel).where(ItemModel.slug == slug)
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Item not found: {slug!r}"
        )
    return ItemDetail(
        slug=row.slug,
        market_hash_name=row.market_hash_name,
        display_name=row.display_name,
        item_type=row.item_type,
        weapon_name=row.weapon_name,
        skin_name=row.skin_name,
        wear=row.wear,
        is_stattrak=row.is_stattrak,
        is_souvenir=row.is_souvenir,
    )


@router.get("/items/{slug}/price", response_model=PriceResponse)
def get_item_price(slug: str) -> PriceResponse:
    """Latest per-source price for an item.

    Driven by ``observation_log`` (one row per ``(item, source)`` pair
    advanced on every successful poll, regardless of whether the dedup
    gate let a ``prices`` row through). For each row we look up the
    most recent ``prices`` row via a LATERAL subquery and surface two
    timestamps:

    - ``last_polled_at`` — the last successful poll (the staleness
      signal the bot's 4-hour 🟡 threshold reads).
    - ``last_changed_at`` — the last time ``(price, volume)`` actually
      changed (an informational "price is flat" signal, NOT a
      warning).

    Sources with no ``observation_log`` row for this item are omitted
    — the bot fills those slots with ``never_observed`` from the
    insights layer. Items that no enabled source has yet observed
    return an empty ``sources`` array (200, not 404) so the bot can
    distinguish "I don't track that item" (404 from ``/items/{slug}``)
    from "I track it but have no data yet" (empty list here).
    """
    engine = get_engine()
    with Session(engine) as session:
        item = session.execute(
            select(ItemModel.id, ItemModel.display_name).where(
                ItemModel.slug == slug
            )
        ).first()
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"Item not found: {slug!r}"
            )

        rows = session.execute(
            text(
                """
                SELECT
                    s.name AS source_name,
                    s.denomination,
                    p.price,
                    p.volume,
                    p.timestamp AS last_changed_at,
                    ol.last_observed_at AS last_polled_at
                FROM observation_log ol
                JOIN sources s ON s.id = ol.source_id
                JOIN LATERAL (
                    SELECT timestamp, price, volume
                    FROM prices
                    WHERE item_id = ol.item_id
                      AND source_id = ol.source_id
                    ORDER BY timestamp DESC
                    LIMIT 1
                ) p ON TRUE
                WHERE ol.item_id = :item_id
                  AND s.enabled = TRUE
                ORDER BY s.name
                """
            ),
            {"item_id": item.id},
        ).mappings().all()

    return PriceResponse(
        slug=slug,
        display_name=item.display_name,
        sources=[
            PerSourcePrice(
                source=row["source_name"],
                denomination=row["denomination"],
                price=row["price"],
                volume=row["volume"],
                last_polled_at=row["last_polled_at"],
                last_changed_at=row["last_changed_at"],
            )
            for row in rows
        ],
    )
