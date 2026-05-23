"""Deterministic helpers for public-inventory asset valuation.

Phase A deliberately keeps the LLM out of the data path. The API parses
the Steam inventory URL, fetches the public inventory snapshot from
Pricempire, locates the exact asset id, and computes a baseline USD range
from local market data. The bot only renders the structured result.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.naming import slugify

PRICEMPIRE_BASE_URL = "https://api.pricempire.com"
PRICEMPIRE_INVENTORY_PATH = "/v4/paid/inventory"
STEAM_COMMUNITY_BASE_URL = "https://steamcommunity.com"
CS2_APP_ID = "730"
CS2_CONTEXT_ID = "2"

_STEAM_ID64_RE = re.compile(r"^7656\d{13}$")
_INVENTORY_FRAGMENT_RE = re.compile(r"^(?P<app_id>\d+)_(?P<context_id>\d+)_(?P<asset_id>\d+)$")
_CENTS = Decimal("0.01")


class InventoryLinkError(ValueError):
    """The supplied URL is not a supported Steam inventory item link."""


class InventoryUnavailableError(RuntimeError):
    """The inventory/profile could not be read as a public inventory."""


class InventoryAssetNotFoundError(RuntimeError):
    """Pricempire returned the inventory, but not the requested asset id."""


@dataclass(frozen=True)
class InventoryItemReference:
    steam_id: str | None
    vanity_id: str | None
    app_id: str
    context_id: str
    asset_id: str


@dataclass(frozen=True)
class PricePoint:
    source: str
    source_family: str
    price: Decimal
    volume: int | None
    observed_at: str | None


def parse_inventory_item_url(url: str) -> InventoryItemReference:
    """Parse a Steam inventory item URL.

    Supported shapes:
    - https://steamcommunity.com/profiles/<steamid64>/inventory/#730_2_<asset>
    - https://steamcommunity.com/id/<vanity>/inventory/#730_2_<asset>
    """
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host not in {"steamcommunity.com", "www.steamcommunity.com"}:
        raise InventoryLinkError("Expected a steamcommunity.com inventory link.")

    fragment_match = _INVENTORY_FRAGMENT_RE.match(parsed.fragment or "")
    if fragment_match is None:
        raise InventoryLinkError(
            "Expected an inventory item fragment like #730_2_<asset_id>."
        )
    app_id = fragment_match.group("app_id")
    context_id = fragment_match.group("context_id")
    asset_id = fragment_match.group("asset_id")
    if app_id != CS2_APP_ID or context_id != CS2_CONTEXT_ID:
        raise InventoryLinkError(
            "Only CS2 inventory links with fragment #730_2_<asset_id> "
            "are supported."
        )

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[2] != "inventory":
        raise InventoryLinkError(
            "Expected /profiles/<steamid>/inventory/ or "
            "/id/<vanity>/inventory/."
        )

    if parts[0] == "profiles" and _STEAM_ID64_RE.match(parts[1]):
        return InventoryItemReference(
            steam_id=parts[1],
            vanity_id=None,
            app_id=app_id,
            context_id=context_id,
            asset_id=asset_id,
        )
    if parts[0] == "id" and parts[1]:
        return InventoryItemReference(
            steam_id=None,
            vanity_id=parts[1],
            app_id=app_id,
            context_id=context_id,
            asset_id=asset_id,
        )

    raise InventoryLinkError("Could not find a SteamID64 or vanity id.")


def resolve_steam_id(reference: InventoryItemReference) -> str:
    """Resolve a parsed reference to SteamID64.

    Numeric profile URLs already carry the SteamID64. Vanity URLs are
    resolved through Steam Community's public XML profile surface; no
    Steam Web API key is required.
    """
    if reference.steam_id is not None:
        return reference.steam_id
    if reference.vanity_id is None:
        raise InventoryLinkError("No Steam profile identifier found.")

    url = f"{STEAM_COMMUNITY_BASE_URL}/id/{reference.vanity_id}"
    try:
        response = httpx.get(
            url,
            params={"xml": 1},
            follow_redirects=True,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise InventoryUnavailableError(
            "Could not resolve that Steam vanity profile."
        ) from exc
    if response.status_code >= 400:
        raise InventoryUnavailableError(
            "Could not resolve that Steam vanity profile."
        )
    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError as exc:
        raise InventoryUnavailableError(
            "Steam returned an unreadable vanity profile response."
        ) from exc
    steam_id = root.findtext("steamID64")
    if steam_id is None or _STEAM_ID64_RE.match(steam_id) is None:
        raise InventoryUnavailableError(
            "Could not resolve that Steam vanity profile to a SteamID64."
        )
    return steam_id


def fetch_pricempire_inventory(steam_id: str, *, force: bool = False) -> dict[str, Any]:
    """Fetch one public inventory from Pricempire.

    The response is decoded with ``Decimal`` for JSON floats so exact
    float values can be compared in independently researched fixtures.
    """
    api_key = os.environ.get("PRICEMPIRE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "PRICEMPIRE_API_KEY environment variable is not set for the API."
        )

    params: dict[str, Any] = {"steam_id": steam_id, "app_id": CS2_APP_ID}
    if force:
        params["force"] = "true"
    try:
        with httpx.Client(
            base_url=PRICEMPIRE_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0),
        ) as client:
            response = client.get(PRICEMPIRE_INVENTORY_PATH, params=params)
    except httpx.RequestError as exc:
        raise InventoryUnavailableError(
            "Pricempire inventory lookup failed before a response arrived."
        ) from exc

    if response.status_code in {400, 403, 404}:
        raise InventoryUnavailableError(
            "That Steam inventory/profile is private or could not be read."
        )
    if response.status_code == 401:
        raise RuntimeError(
            "Pricempire rejected PRICEMPIRE_API_KEY for inventory lookup."
        )
    if response.status_code >= 500:
        raise InventoryUnavailableError(
            "Pricempire inventory lookup is temporarily unavailable."
        )
    if response.status_code >= 400:
        raise InventoryUnavailableError(
            f"Pricempire inventory lookup returned HTTP {response.status_code}."
        )
    return json.loads(response.text, parse_float=Decimal)


def find_inventory_asset(inventory: dict[str, Any], asset_id: str) -> dict[str, Any]:
    items = inventory.get("items")
    if not isinstance(items, list):
        raise InventoryUnavailableError(
            "Pricempire returned an inventory without an items list."
        )
    for item in items:
        if str(item.get("asset_id")) == asset_id:
            return item
    raise InventoryAssetNotFoundError(
        "The inventory was readable, but that asset id was not present."
    )


def load_latest_usd_price_points(
    session: Session, market_hash_name: str
) -> list[PricePoint]:
    """Return latest local USD price points for a market hash name."""
    rows = session.execute(
        text(
            """
            WITH target AS (
                SELECT id
                FROM items
                WHERE market_hash_name = :market_hash_name
            ),
            direct_rows AS (
                SELECT
                    s.name AS source,
                    'direct' AS source_family,
                    p.price,
                    p.volume,
                    ol.last_observed_at AS observed_at
                FROM target t
                JOIN observation_log ol ON ol.item_id = t.id
                JOIN sources s ON s.id = ol.source_id
                JOIN LATERAL (
                    SELECT price, volume, timestamp
                    FROM prices
                    WHERE item_id = ol.item_id
                      AND source_id = ol.source_id
                    ORDER BY timestamp DESC
                    LIMIT 1
                ) p ON TRUE
                WHERE s.enabled = TRUE
                  AND s.denomination = 'usd'
            ),
            pricempire_rows AS (
                SELECT DISTINCT ON (po.source_id)
                    s.name AS source,
                    'pricempire' AS source_family,
                    po.price,
                    po.count AS volume,
                    COALESCE(pol.last_observed_at, po.last_checked_at, po.timestamp)
                        AS observed_at,
                    po.timestamp
                FROM target t
                JOIN pricempire_observations po ON po.item_id = t.id
                JOIN sources s ON s.id = po.source_id
                LEFT JOIN pricempire_observation_log pol
                    ON pol.item_id = po.item_id
                   AND pol.source_id = po.source_id
                WHERE s.enabled = TRUE
                  AND s.denomination = 'usd'
                ORDER BY po.source_id, po.timestamp DESC
            )
            SELECT source, source_family, price, volume, observed_at
            FROM direct_rows
            UNION ALL
            SELECT source, source_family, price, volume, observed_at
            FROM pricempire_rows
            ORDER BY source_family, source
            """
        ),
        {"market_hash_name": market_hash_name},
    ).mappings()
    return [
        PricePoint(
            source=row["source"],
            source_family=row["source_family"],
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


def build_value_gauge(price_points: list[PricePoint]) -> dict[str, Any] | None:
    """Compute a baseline USD range from available local price points."""
    if not price_points:
        return None
    prices = sorted(point.price for point in price_points)
    low = prices[0]
    high = prices[-1]
    mid = _median(prices)
    source_count = len(price_points)
    confidence = "high" if source_count >= 3 else "medium" if source_count == 2 else "low"
    return {
        "currency": "usd",
        "low": _money(low),
        "mid": _money(mid),
        "high": _money(high),
        "source_count": source_count,
        "confidence": confidence,
        "method": (
            "Median/min/max of latest local USD price points for the "
            "asset's market_hash_name. Steam Wallet credit is excluded."
        ),
        "limitations": (
            "This is a market-name baseline. Float, seed, sticker, and "
            "charm premiums are surfaced as attributes but are not repriced "
            "until stronger independent known-answer fixtures calibrate them."
        ),
    }


def build_inventory_valuation_response(
    *,
    reference: InventoryItemReference,
    steam_id: str,
    asset: dict[str, Any],
    price_points: list[PricePoint],
) -> dict[str, Any]:
    item = asset.get("item") or {}
    market_hash_name = item.get("market_hash_name")
    value_gauge = build_value_gauge(price_points)
    status = "ok" if value_gauge is not None else "no_value_data"
    explanation = _explanation(status=status, market_hash_name=market_hash_name)
    return {
        "status": status,
        "reason": None if status == "ok" else "no_local_price_data",
        "message": explanation,
        "reference": {
            "steam_id": steam_id,
            "app_id": reference.app_id,
            "context_id": reference.context_id,
            "asset_id": reference.asset_id,
        },
        "asset": {
            "asset_id": str(asset.get("asset_id")),
            "market_hash_name": market_hash_name,
            "slug": slugify(market_hash_name) if market_hash_name else None,
            "float_value": _decimal_text(asset.get("float_value")),
            "paint_seed": asset.get("paint_seed"),
            "paint_id": item.get("paint_id"),
            "low_rank": asset.get("low_rank"),
            "high_rank": asset.get("high_rank"),
            "stickers": [_shape_sticker(row) for row in asset.get("stickers") or []],
            "charms": asset.get("charms") or [],
        },
        "value_gauge": value_gauge,
        "price_points": [
            {
                "source": point.source,
                "source_family": point.source_family,
                "price": _money(point.price),
                "volume": point.volume,
                "observed_at": point.observed_at,
            }
            for point in price_points
        ],
    }


def unreadable_response(reason: str, message: str) -> dict[str, Any]:
    return {
        "status": "unreadable",
        "reason": reason,
        "message": message,
        "reference": None,
        "asset": None,
        "value_gauge": None,
        "price_points": [],
    }


def _median(values: list[Decimal]) -> Decimal:
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint].quantize(_CENTS, rounding=ROUND_HALF_UP)
    return ((values[midpoint - 1] + values[midpoint]) / 2).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _shape_sticker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "slot": row.get("slot"),
        "wear": _decimal_text(row.get("wear")),
        "sticker_id": row.get("stickerId") or row.get("sticker_id"),
    }


def _explanation(*, status: str, market_hash_name: str | None) -> str:
    if status == "ok":
        return (
            f"Exact asset attributes were read from the public inventory. "
            f"The value gauge for {market_hash_name} is a deterministic "
            f"USD baseline from local market data; asset-specific premiums "
            f"remain fixture-gated."
        )
    return (
        f"Exact asset attributes were read for {market_hash_name}, but no "
        f"local USD market rows are available for a value gauge."
    )
