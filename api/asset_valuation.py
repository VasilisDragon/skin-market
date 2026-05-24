"""Deterministic helpers for public-inventory asset market baselines.

The API parses the Steam inventory URL, fetches the public inventory
snapshot from Pricempire, locates the exact asset id, and computes a
market-name USD baseline from local market data. The bot only renders
the structured result.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from cs2_inspect_lite import decode_inspect_url, is_classic, is_masked
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.naming import slugify

PRICEMPIRE_BASE_URL = "https://api.pricempire.com"
PRICEMPIRE_INVENTORY_PATH = "/v4/paid/inventory"
CSGO_API_BASE_URL = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en"
)
STEAM_COMMUNITY_BASE_URL = "https://steamcommunity.com"
CS2_APP_ID = "730"
CS2_CONTEXT_ID = "2"

_STEAM_ID64_RE = re.compile(r"^7656\d{13}$")
_INVENTORY_FRAGMENT_RE = re.compile(r"^(?P<app_id>\d+)_(?P<context_id>\d+)_(?P<asset_id>\d+)$")
_CENTS = Decimal("0.01")
BASELINE_WIDE_SPREAD_RATIO = Decimal("5.0")
BASELINE_MIN_RELIABLE_SOURCE_COUNT = 3
_WEAR_BANDS: tuple[tuple[str, str, Decimal, Decimal], ...] = (
    ("factory_new", "Factory New", Decimal("0"), Decimal("0.07")),
    ("minimal_wear", "Minimal Wear", Decimal("0.07"), Decimal("0.15")),
    ("field_tested", "Field-Tested", Decimal("0.15"), Decimal("0.38")),
    ("well_worn", "Well-Worn", Decimal("0.38"), Decimal("0.45")),
    ("battle_scarred", "Battle-Scarred", Decimal("0.45"), Decimal("1")),
)
_LOW_FLOAT_POSITION_PCT = Decimal("15")

PREMIUM_SIGNAL_AVAILABILITY: dict[str, dict[str, Any]] = {
    "low_float_for_wear_band": {
        "status": "not_available",
        "source": None,
        "explanation": (
            "Exact float is known, but no integrated per-float comparable-sales "
            "source or confirmed sales corpus is available."
        ),
    },
    "applied_stickers": {
        "status": "not_available",
        "source": None,
        "explanation": (
            "Applied stickers are known, but the system has no integrated "
            "source for applied-sticker sale premiums."
        ),
    },
    "applied_charms": {
        "status": "not_available",
        "source": None,
        "explanation": (
            "Applied charms are known, but the system has no integrated "
            "source for applied-charm sale premiums."
        ),
    },
    "pattern_sensitive_family": {
        "status": "not_available",
        "source": None,
        "explanation": (
            "The skin family can be pattern-sensitive, but no approved "
            "pattern-tier or confirmed-sales source is integrated."
        ),
    },
    "phase_already_in_market_name": {
        "status": "covered_by_market_baseline",
        "source": "market_hash_name",
        "explanation": (
            "This phase is already separated in the market_hash_name, so the "
            "generic market baseline reflects that named variant. No extra "
            "phase premium is computed."
        ),
    },
    "rank_present": {
        "status": "not_available",
        "source": None,
        "explanation": (
            "Rank metadata is present, but the system has no integrated source "
            "that converts rank into a validated sale premium."
        ),
    },
}
_PREMIUM_DRIVER_LABELS: dict[str, str] = {
    "low_float_for_wear_band": "low float for wear band",
    "applied_stickers": "applied stickers",
    "applied_charms": "applied charms",
    "pattern_sensitive_family": "pattern-sensitive skin family",
    "phase_already_in_market_name": "phase already in market name",
    "rank_present": "rank metadata",
}


class InventoryLinkError(ValueError):
    """The supplied URL is not a supported Steam inventory item link."""


class InventoryUnavailableError(RuntimeError):
    """The inventory/profile could not be read as a public inventory."""


class InventoryAssetNotFoundError(RuntimeError):
    """Pricempire returned the inventory, but not the requested asset id."""


class InspectLinkError(ValueError):
    """The supplied URL is not a supported CS2 inspect link."""


class InspectLinkUnsupportedError(RuntimeError):
    """The inspect link is valid but needs an out-of-scope resolver."""


class CSGOReferenceUnavailableError(RuntimeError):
    """The public CS2 schema reference could not be loaded."""


@dataclass(frozen=True)
class InventoryItemReference:
    steam_id: str | None
    vanity_id: str | None
    app_id: str
    context_id: str
    asset_id: str


@dataclass(frozen=True)
class InventoryOwnerReference:
    steam_id: str | None
    vanity_id: str | None
    app_id: str
    context_id: str


@dataclass(frozen=True)
class PricePoint:
    source: str
    source_family: str
    price: Decimal
    volume: int | None
    observed_at: str | None


@dataclass(frozen=True)
class CSGOReferenceData:
    skins_not_grouped: list[dict[str, Any]]
    stickers_by_id: dict[str, dict[str, Any]]
    keychains_by_id: dict[str, dict[str, Any]]


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


def parse_inventory_owner_url(url: str) -> InventoryOwnerReference:
    """Parse a Steam inventory URL for whole-inventory baseline summaries."""
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host not in {"steamcommunity.com", "www.steamcommunity.com"}:
        raise InventoryLinkError("Expected a steamcommunity.com inventory link.")

    if parsed.fragment:
        fragment_match = _INVENTORY_FRAGMENT_RE.match(parsed.fragment)
        if fragment_match is None:
            raise InventoryLinkError(
                "Expected an inventory URL, optionally with #730_2_<asset_id>."
            )
        app_id = fragment_match.group("app_id")
        context_id = fragment_match.group("context_id")
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
        return InventoryOwnerReference(
            steam_id=parts[1],
            vanity_id=None,
            app_id=CS2_APP_ID,
            context_id=CS2_CONTEXT_ID,
        )
    if parts[0] == "id" and parts[1]:
        return InventoryOwnerReference(
            steam_id=None,
            vanity_id=parts[1],
            app_id=CS2_APP_ID,
            context_id=CS2_CONTEXT_ID,
        )

    raise InventoryLinkError("Could not find a SteamID64 or vanity id.")


def resolve_steam_id(reference: InventoryItemReference | InventoryOwnerReference) -> str:
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


def decode_modern_inspect_link(inspect_url: str) -> Any:
    """Decode a modern CS2 inspect link without Steam account state.

    March 2026+ masked/hybrid links self-encode item properties. Classic
    ``S...A...D<decimal>`` links are only pointers; resolving them needs
    the Steam Game Coordinator and a Steam account, so the route declines
    them explicitly under the session scope boundary.
    """
    inspect_url = inspect_url.strip()
    if not inspect_url:
        raise InspectLinkError("Expected a CS2 inspect link.")
    if is_classic(inspect_url):
        raise InspectLinkUnsupportedError(
            "Legacy inspect links only contain Steam Game Coordinator "
            "pointers. Resolving them requires a Steam account/GC session, "
            "which is outside this session's scope."
        )
    if not is_masked(inspect_url):
        raise InspectLinkError(
            "Expected a modern CS2 inspect link with an encoded payload."
        )
    decoded = decode_inspect_url(inspect_url)
    if decoded is None:
        raise InspectLinkError("Could not decode that CS2 inspect link.")
    return decoded


@lru_cache(maxsize=1)
def fetch_csgo_reference_data() -> CSGOReferenceData:
    """Load public CS2 schema data used to name decoded inspect links."""
    try:
        with httpx.Client(
            base_url=CSGO_API_BASE_URL,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as client:
            skins_response = client.get("/skins_not_grouped.json")
            inventory_response = client.get("/inventory.json")
    except httpx.RequestError as exc:
        raise CSGOReferenceUnavailableError(
            "Could not load the public CS2 schema reference."
        ) from exc

    if skins_response.status_code >= 400 or inventory_response.status_code >= 400:
        raise CSGOReferenceUnavailableError(
            "The public CS2 schema reference returned an HTTP error."
        )

    skins = skins_response.json()
    inventory = inventory_response.json()
    if not isinstance(skins, list) or not isinstance(inventory, dict):
        raise CSGOReferenceUnavailableError(
            "The public CS2 schema reference had an unexpected shape."
        )

    return CSGOReferenceData(
        skins_not_grouped=skins,
        stickers_by_id=inventory.get("stickers") or {},
        keychains_by_id=inventory.get("keychains") or {},
    )


def resolve_decoded_market_hash_name(
    decoded: Any, reference_data: CSGOReferenceData
) -> str | None:
    """Map decoded defindex/paint/wear/quality to a market hash name."""
    wear_name = _wear_name(decoded.paintwear)
    is_stattrak = _is_stattrak_quality(decoded.quality)
    is_souvenir = _is_souvenir_quality(decoded.quality)

    for row in reference_data.skins_not_grouped:
        weapon = row.get("weapon") or {}
        wear = row.get("wear") or {}
        if int(weapon.get("weapon_id") or -1) != int(decoded.defindex):
            continue
        if int(row.get("paint_index") or -1) != int(decoded.paintindex):
            continue
        if wear.get("name") != wear_name:
            continue
        if bool(row.get("stattrak")) != is_stattrak:
            continue
        if bool(row.get("souvenir")) != is_souvenir:
            continue
        market_hash_name = row.get("market_hash_name")
        return str(market_hash_name) if market_hash_name else None
    return None


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


def build_market_baseline(price_points: list[PricePoint]) -> dict[str, Any] | None:
    """Compute a market-name USD baseline from available local price points."""
    if not price_points:
        return None
    prices = sorted(point.price for point in price_points)
    low = prices[0]
    high = prices[-1]
    mid = _median(prices)
    source_count = len(price_points)
    reliability = _baseline_reliability(
        low=low,
        high=high,
        source_count=source_count,
    )
    confidence = (
        "high"
        if reliability == "reliable" and source_count >= 3
        else "low"
        if reliability != "reliable"
        else "medium"
    )
    baseline = {
        "currency": "usd",
        "low": _money(low),
        "high": _money(high),
        "source_count": source_count,
        "confidence": confidence,
        "baseline_reliability": reliability,
        "reliability": _baseline_reliability_details(
            low=low,
            high=high,
            source_count=source_count,
            reliability=reliability,
        ),
        "method": (
            "Median/min/max of latest local USD price points for the "
            "asset's market_hash_name. Steam Wallet credit is excluded."
        ),
        "limitations": (
            "This is a market-name baseline. Float, seed, sticker, and "
            "charm premiums are surfaced as attributes but are not repriced "
            "until stronger independent known-answer fixtures calibrate them. "
            "Do not infer collector upside, buyer demand, or a premium-adjusted "
            "value from these attributes."
        ),
    }
    if reliability == "reliable":
        baseline["mid"] = _money(mid)
    return baseline


def build_asset_evidence(asset: dict[str, Any]) -> dict[str, Any]:
    """Describe exact premium drivers without computing any premium value."""
    market_hash_name = str(asset.get("market_hash_name") or "")
    stickers = asset.get("stickers") or []
    charms = asset.get("charms") or []
    wear_band = _wear_band_evidence(asset.get("float_value"))
    pattern_families = _pattern_families(market_hash_name)
    phase = _phase_marker(market_hash_name)
    ranks = {
        "low_rank": asset.get("low_rank"),
        "high_rank": asset.get("high_rank"),
    }
    attributes = {
        "market_hash_name": market_hash_name or None,
        "float_value": _decimal_text(asset.get("float_value")),
        "wear_band": wear_band,
        "paint_seed": asset.get("paint_seed"),
        "paint_id": asset.get("paint_id"),
        "is_stattrak": bool(asset.get("is_stattrak"))
        or _market_name_is_stattrak(market_hash_name),
        "is_souvenir": bool(asset.get("is_souvenir"))
        or _market_name_is_souvenir(market_hash_name),
        "ranks": ranks if any(value is not None for value in ranks.values()) else None,
        "stickers": stickers,
        "charms": charms,
    }
    flags = [
        _driver_flag(
            "low_float_for_wear_band",
            bool(wear_band and wear_band.get("float_position") == "low"),
            "low" if wear_band and wear_band.get("float_position") == "low" else "not_low",
            (
                "Float is in the lowest "
                f"{_LOW_FLOAT_POSITION_PCT}% of its wear band."
                if wear_band and wear_band.get("float_position") == "low"
                else "Float is not in the configured low-float slice of its wear band."
            ),
        ),
        _driver_flag(
            "applied_stickers",
            bool(stickers),
            f"{len(stickers)}_stickers" if stickers else "none",
            (
                f"{len(stickers)} applied sticker(s) are present."
                if stickers
                else "No applied stickers are present."
            ),
        ),
        _driver_flag(
            "applied_charms",
            bool(charms),
            f"{len(charms)}_charms" if charms else "none",
            (
                f"{len(charms)} applied charm(s) are present."
                if charms
                else "No applied charms are present."
            ),
        ),
        _driver_flag(
            "pattern_sensitive_family",
            bool(pattern_families),
            ",".join(row["code"] for row in pattern_families) if pattern_families else "none",
            (
                "Market name matches pattern-sensitive family: "
                + ", ".join(row["name"] for row in pattern_families)
                if pattern_families
                else "Market name does not match the configured pattern-sensitive families."
            ),
        ),
        _driver_flag(
            "phase_already_in_market_name",
            phase is not None,
            phase["code"] if phase else "none",
            (
                f"Doppler/Gamma phase is already named as {phase['name']}."
                if phase
                else "No Doppler/Gamma phase marker is separated in the market name."
            ),
        ),
        _driver_flag(
            "rank_present",
            attributes["ranks"] is not None,
            "ranked" if attributes["ranks"] is not None else "none",
            (
                "Rank metadata is present on the asset."
                if attributes["ranks"] is not None
                else "No rank metadata is present on the asset."
            ),
        ),
    ]
    present_codes = [flag["code"] for flag in flags if flag["present"]]
    return {
        "attributes": attributes,
        "driver_flags": flags,
        "signal_availability": {
            code: PREMIUM_SIGNAL_AVAILABILITY[code] for code in present_codes
        },
        "summary": _evidence_summary(market_hash_name, flags),
    }


def build_inventory_baseline_response(
    *,
    reference: InventoryItemReference,
    steam_id: str,
    asset: dict[str, Any],
    price_points: list[PricePoint],
) -> dict[str, Any]:
    item = asset.get("item") or {}
    market_hash_name = item.get("market_hash_name")
    market_baseline = build_market_baseline(price_points)
    status = "ok" if market_baseline is not None else "no_value_data"
    explanation = _explanation(status=status, market_hash_name=market_hash_name)
    asset_payload = {
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
    }
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
        "asset": asset_payload,
        "evidence": build_asset_evidence(asset_payload),
        "market_baseline": market_baseline,
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


def build_inspect_baseline_response(
    *,
    inspect_url: str,
    decoded: Any,
    market_hash_name: str | None,
    reference_data: CSGOReferenceData | None,
    price_points: list[PricePoint],
) -> dict[str, Any]:
    market_baseline = build_market_baseline(price_points)
    if market_hash_name is None:
        status = "no_value_data"
        reason = "market_hash_name_unresolved"
        explanation = (
            "Exact asset attributes were decoded from the inspect link, "
            "but the market_hash_name could not be resolved from the CS2 "
            "schema, so no market baseline is available."
        )
    else:
        status = "ok" if market_baseline is not None else "no_value_data"
        reason = None if status == "ok" else "no_local_price_data"
        explanation = _inspect_explanation(
            status=status,
            market_hash_name=market_hash_name,
        )

    asset_payload = {
        "asset_id": str(decoded.itemid) if decoded.itemid else None,
        "market_hash_name": market_hash_name,
        "slug": slugify(market_hash_name) if market_hash_name else None,
        "float_value": _float_text(decoded.paintwear),
        "paint_seed": decoded.paintseed,
        "paint_id": decoded.paintindex,
        "defindex": decoded.defindex,
        "rarity": decoded.rarity,
        "quality": decoded.quality,
        "is_stattrak": _is_stattrak_quality(decoded.quality),
        "is_souvenir": _is_souvenir_quality(decoded.quality),
        "stickers": [
            _shape_decoded_sticker(row, reference_data)
            for row in decoded.stickers
        ],
        "charms": [
            _shape_decoded_keychain(row, reference_data)
            for row in decoded.keychains
        ],
    }
    return {
        "status": status,
        "reason": reason,
        "message": explanation,
        "reference": {
            "inspect_url": inspect_url,
            "inspect_link_format": "modern_encoded",
            "decoder": "cs2-inspect-lite",
        },
        "asset": asset_payload,
        "evidence": build_asset_evidence(asset_payload),
        "market_baseline": market_baseline,
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


def build_inventory_summary_response(
    *,
    reference: InventoryOwnerReference,
    steam_id: str,
    inventory: dict[str, Any],
    price_points_by_name: dict[str, list[PricePoint]],
) -> dict[str, Any]:
    items = inventory.get("items")
    if not isinstance(items, list):
        raise InventoryUnavailableError(
            "Pricempire returned an inventory without an items list."
        )

    total_low = Decimal("0")
    total_mid = Decimal("0")
    total_high = Decimal("0")
    reliable_mid_count = 0
    priced_items: list[dict[str, Any]] = []
    unpriced_items: list[dict[str, Any]] = []
    stickered_count = 0

    for asset in items:
        item = asset.get("item") or {}
        market_hash_name = item.get("market_hash_name")
        if not market_hash_name:
            continue
        stickers = asset.get("stickers") or []
        if stickers:
            stickered_count += 1
        baseline = build_market_baseline(
            price_points_by_name.get(str(market_hash_name), [])
        )
        asset_payload = {
            "asset_id": str(asset.get("asset_id")),
            "market_hash_name": market_hash_name,
            "float_value": _decimal_text(asset.get("float_value")),
            "paint_seed": asset.get("paint_seed"),
            "paint_id": item.get("paint_id"),
            "low_rank": asset.get("low_rank"),
            "high_rank": asset.get("high_rank"),
            "stickers": [_shape_sticker(row) for row in stickers],
            "charms": asset.get("charms") or [],
        }
        evidence = build_asset_evidence(asset_payload)
        shaped = {
            "asset_id": str(asset.get("asset_id")),
            "market_hash_name": market_hash_name,
            "float_value": _decimal_text(asset.get("float_value")),
            "paint_seed": asset.get("paint_seed"),
            "paint_id": item.get("paint_id"),
            "sticker_count": len(stickers),
            "market_baseline": baseline,
            "evidence": evidence,
        }
        if baseline is None:
            unpriced_items.append(shaped)
            continue
        total_low += Decimal(baseline["low"])
        total_high += Decimal(baseline["high"])
        if baseline.get("mid") is not None:
            total_mid += Decimal(baseline["mid"])
            reliable_mid_count += 1
            shaped["baseline_spread_pct"] = _pct(
                (Decimal(baseline["high"]) - Decimal(baseline["low"]))
                / Decimal(baseline["mid"])
                * 100
            )
        else:
            shaped["baseline_spread_pct"] = None
        priced_items.append(shaped)

    priced_items.sort(
        key=lambda row: _baseline_sort_value(row["market_baseline"]),
        reverse=True,
    )
    largest_spread_items = sorted(
        priced_items,
        key=lambda row: _baseline_spread_sort_value(row["market_baseline"]),
        reverse=True,
    )
    status = "ok" if priced_items else "no_value_data"
    priced_count = len(priced_items)
    unpriced_count = len(unpriced_items)
    total_count = priced_count + unpriced_count
    portfolio_reliability = _portfolio_baseline_reliability(
        item_baselines=[row["market_baseline"] for row in priced_items],
        low=total_low,
        high=total_high,
        priced_count=priced_count,
        reliable_mid_count=reliable_mid_count,
    )
    message = (
        f"Found market baselines for {priced_count} of {total_count} CS2 "
        "inventory assets. Totals are market-name baselines and do not include "
        "float, seed, sticker, or charm premiums."
    )
    if status == "no_value_data":
        message = (
            "Exact inventory assets were read, but no local USD market rows "
            "were available for a portfolio baseline."
        )
    evidence = _portfolio_evidence(
        [*priced_items, *unpriced_items],
        priced_count=priced_count,
        unpriced_count=unpriced_count,
    )
    portfolio_baseline = None
    if priced_items:
        portfolio_baseline = {
            "currency": "usd",
            "low": _money(total_low),
            "high": _money(total_high),
            "priced_count": priced_count,
            "unpriced_count": unpriced_count,
            "stickered_count": stickered_count,
            "top_item_share_pct": (
                _pct(
                    Decimal(priced_items[0]["market_baseline"]["mid"])
                    / total_mid
                    * 100
                )
                if portfolio_reliability == "reliable"
                and total_mid > 0
                and priced_items[0]["market_baseline"].get("mid") is not None
                else None
            ),
            "baseline_reliability": portfolio_reliability,
            "reliability": _portfolio_reliability_details(
                item_baselines=[row["market_baseline"] for row in priced_items],
                low=total_low,
                high=total_high,
                priced_count=priced_count,
                reliable_mid_count=reliable_mid_count,
                reliability=portfolio_reliability,
            ),
            "method": (
                "Sum of each priced asset's market-name low/high baseline "
                "from latest local USD rows. Mid is shown only when every "
                "included item baseline is reliable. Steam Wallet credit is "
                "excluded."
            ),
            "limitations": (
                "This is a portfolio market baseline. It does not reprice "
                "float, seed, sticker, charm, or pattern premiums."
            ),
        }
        if portfolio_reliability == "reliable":
            portfolio_baseline["mid"] = _money(total_mid)

    return {
        "status": status,
        "reason": None if status == "ok" else "no_local_price_data",
        "message": message,
        "reference": {
            "steam_id": steam_id,
            "app_id": reference.app_id,
            "context_id": reference.context_id,
        },
        "evidence": evidence,
        "portfolio_baseline": portfolio_baseline,
        "top_items": priced_items[:10],
        "largest_spread_items": largest_spread_items[:10],
        "unpriced_sample": unpriced_items[:10],
    }


def unreadable_response(reason: str, message: str) -> dict[str, Any]:
    return {
        "status": "unreadable",
        "reason": reason,
        "message": message,
        "reference": None,
        "asset": None,
        "evidence": None,
        "market_baseline": None,
        "price_points": [],
    }


def unreadable_inventory_summary_response(reason: str, message: str) -> dict[str, Any]:
    return {
        "status": "unreadable",
        "reason": reason,
        "message": message,
        "reference": None,
        "evidence": None,
        "portfolio_baseline": None,
        "top_items": [],
        "largest_spread_items": [],
        "unpriced_sample": [],
    }


def _median(values: list[Decimal]) -> Decimal:
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint].quantize(_CENTS, rounding=ROUND_HALF_UP)
    return ((values[midpoint - 1] + values[midpoint]) / 2).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )


def _baseline_reliability(
    *,
    low: Decimal,
    high: Decimal,
    source_count: int,
) -> str:
    if _high_low_ratio(low=low, high=high) >= BASELINE_WIDE_SPREAD_RATIO:
        return "wide_spread"
    if source_count < BASELINE_MIN_RELIABLE_SOURCE_COUNT:
        return "thin_sources"
    return "reliable"


def _baseline_reliability_details(
    *,
    low: Decimal,
    high: Decimal,
    source_count: int,
    reliability: str,
) -> dict[str, Any]:
    ratio = _high_low_ratio(low=low, high=high)
    if reliability == "wide_spread":
        message = (
            f"Sources range {_money(low)}-{_money(high)} USD; high/low spread "
            "is too wide for a usable midpoint."
        )
    elif reliability == "thin_sources":
        message = (
            f"Only {source_count} USD source(s) are available; this is too thin "
            "for a usable midpoint."
        )
    else:
        message = "Source count and spread are sufficient to show a midpoint."
    return {
        "status": reliability,
        "wide_spread_ratio_threshold": str(BASELINE_WIDE_SPREAD_RATIO),
        "min_reliable_source_count": BASELINE_MIN_RELIABLE_SOURCE_COUNT,
        "high_low_ratio": _ratio_text(ratio),
        "mid_suppressed": reliability != "reliable",
        "message": message,
    }


def _high_low_ratio(*, low: Decimal, high: Decimal) -> Decimal:
    if low <= 0:
        return Decimal("Infinity") if high > 0 else Decimal("1")
    return high / low


def _ratio_text(value: Decimal) -> str:
    if value.is_infinite():
        return "Infinity"
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _pct(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _float_text(value: float | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _wear_band_evidence(value: Any) -> dict[str, Any] | None:
    text = _decimal_text(value)
    if text is None:
        return None
    try:
        wear = Decimal(text)
    except Exception:
        return None
    for code, name, low, high in _WEAR_BANDS:
        if low <= wear < high or (code == "battle_scarred" and wear <= high):
            position_pct = (wear - low) / (high - low) * Decimal("100")
            return {
                "code": code,
                "name": name,
                "min_float": str(low),
                "max_float": str(high),
                "position_pct": _pct(position_pct),
                "float_position": (
                    "low"
                    if position_pct <= _LOW_FLOAT_POSITION_PCT
                    else "standard"
                ),
                "low_float_threshold_pct": str(_LOW_FLOAT_POSITION_PCT),
            }
    return None


def _driver_flag(
    code: str,
    present: bool,
    category: str,
    explanation: str,
) -> dict[str, Any]:
    availability = PREMIUM_SIGNAL_AVAILABILITY[code]
    return {
        "code": code,
        "label": _PREMIUM_DRIVER_LABELS[code],
        "present": present,
        "category": category,
        "explanation": explanation,
        "signal_status": availability["status"],
    }


def _market_name_is_stattrak(market_hash_name: str) -> bool:
    return "stattrak" in market_hash_name.lower()


def _market_name_is_souvenir(market_hash_name: str) -> bool:
    return market_hash_name.lower().startswith("souvenir ")


def _pattern_families(market_hash_name: str) -> list[dict[str, str]]:
    name = market_hash_name.lower()
    families: list[dict[str, str]] = []
    if "case hardened" in name or "heat treated" in name:
        families.append({"code": "case_hardened", "name": "Case Hardened"})
    if "marble fade" in name:
        families.append({"code": "marble_fade", "name": "Marble Fade"})
    elif " | fade" in name or name.endswith(" fade"):
        families.append({"code": "fade", "name": "Fade"})
    if "gamma doppler" in name:
        families.append({"code": "gamma_doppler", "name": "Gamma Doppler"})
    elif "doppler" in name:
        families.append({"code": "doppler", "name": "Doppler"})
    if "crimson web" in name:
        families.append({"code": "crimson_web", "name": "Crimson Web"})
    return families


def _phase_marker(market_hash_name: str) -> dict[str, str] | None:
    name = market_hash_name.lower()
    if "doppler" not in name:
        return None
    markers = (
        ("black_pearl", "Black Pearl"),
        ("sapphire", "Sapphire"),
        ("emerald", "Emerald"),
        ("ruby", "Ruby"),
        ("phase_1", "Phase 1"),
        ("phase_2", "Phase 2"),
        ("phase_3", "Phase 3"),
        ("phase_4", "Phase 4"),
    )
    for code, label in markers:
        if label.lower() in name:
            return {"code": code, "name": label}
    return None


def _evidence_summary(market_hash_name: str, flags: list[dict[str, Any]]) -> str:
    present = [flag["label"] for flag in flags if flag["present"]]
    item_name = market_hash_name or "this asset"
    if not present:
        return (
            f"The market baseline for {item_name} is a generic market-name "
            "range. No configured premium drivers were detected, and the "
            "system does not compute any per-asset premium."
        )
    driver_text = ", ".join(present)
    return (
        f"The market baseline for {item_name} is a generic market-name range. "
        f"Premium drivers detected: {driver_text}. The system cannot currently "
        "price those drivers; a real appraisal requires an approved data source "
        "and a confirmed sales corpus."
    )


def _portfolio_evidence(
    items: list[dict[str, Any]],
    *,
    priced_count: int,
    unpriced_count: int,
) -> dict[str, Any]:
    driver_counts = dict.fromkeys(PREMIUM_SIGNAL_AVAILABILITY, 0)
    for item in items:
        evidence = item.get("evidence") or {}
        for flag in evidence.get("driver_flags") or []:
            if flag.get("present"):
                driver_counts[flag["code"]] += 1
    present_driver_counts = {
        code: count for code, count in driver_counts.items() if count
    }
    return {
        "attributes": {
            "priced_count": priced_count,
            "unpriced_count": unpriced_count,
            "total_count": priced_count + unpriced_count,
        },
        "driver_counts": present_driver_counts,
        "signal_availability": {
            code: PREMIUM_SIGNAL_AVAILABILITY[code]
            for code in present_driver_counts
        },
        "summary": _portfolio_evidence_summary(
            present_driver_counts,
            priced_count=priced_count,
            unpriced_count=unpriced_count,
        ),
    }


def _portfolio_evidence_summary(
    driver_counts: dict[str, int],
    *,
    priced_count: int,
    unpriced_count: int,
) -> str:
    total_count = priced_count + unpriced_count
    if not driver_counts:
        return (
            f"The portfolio baseline covers {priced_count} of {total_count} "
            "assets with generic market-name ranges. No configured premium "
            "drivers were detected in the returned sample."
        )
    drivers = ", ".join(
        f"{_PREMIUM_DRIVER_LABELS[code]} ({count})"
        for code, count in driver_counts.items()
    )
    return (
        f"The portfolio baseline covers {priced_count} of {total_count} assets "
        "with generic market-name ranges. Premium drivers detected in the "
        f"returned assets: {drivers}. The system cannot currently price those "
        "drivers without an approved data source and confirmed-sales corpus."
    )


def _baseline_sort_value(baseline: dict[str, Any]) -> Decimal:
    value = baseline.get("mid") or baseline.get("high") or baseline.get("low") or "0"
    return Decimal(str(value))


def _baseline_spread_sort_value(baseline: dict[str, Any]) -> Decimal:
    low = Decimal(str(baseline.get("low") or "0"))
    high = Decimal(str(baseline.get("high") or "0"))
    return _high_low_ratio(low=low, high=high)


def _portfolio_baseline_reliability(
    *,
    item_baselines: list[dict[str, Any]],
    low: Decimal,
    high: Decimal,
    priced_count: int,
    reliable_mid_count: int,
) -> str:
    if not item_baselines:
        return "thin_sources"
    if any(row.get("baseline_reliability") == "wide_spread" for row in item_baselines):
        return "wide_spread"
    if _high_low_ratio(low=low, high=high) >= BASELINE_WIDE_SPREAD_RATIO:
        return "wide_spread"
    if reliable_mid_count < priced_count:
        return "thin_sources"
    return "reliable"


def _portfolio_reliability_details(
    *,
    item_baselines: list[dict[str, Any]],
    low: Decimal,
    high: Decimal,
    priced_count: int,
    reliable_mid_count: int,
    reliability: str,
) -> dict[str, Any]:
    ratio = _high_low_ratio(low=low, high=high)
    if reliability == "wide_spread":
        message = (
            f"Portfolio sources sum to {_money(low)}-{_money(high)} USD; "
            "spread is too wide for a usable total midpoint."
        )
    elif reliability == "thin_sources":
        message = (
            f"{reliable_mid_count} of {priced_count} priced item baseline(s) "
            "have enough source support for a midpoint, so no portfolio "
            "midpoint is shown."
        )
    else:
        message = "All priced item baselines are reliable enough to sum a midpoint."
    return {
        "status": reliability,
        "wide_spread_ratio_threshold": str(BASELINE_WIDE_SPREAD_RATIO),
        "min_reliable_source_count": BASELINE_MIN_RELIABLE_SOURCE_COUNT,
        "high_low_ratio": _ratio_text(ratio),
        "mid_suppressed": reliability != "reliable",
        "item_reliability_counts": {
            "reliable": sum(
                1 for row in item_baselines if row.get("baseline_reliability") == "reliable"
            ),
            "wide_spread": sum(
                1
                for row in item_baselines
                if row.get("baseline_reliability") == "wide_spread"
            ),
            "thin_sources": sum(
                1
                for row in item_baselines
                if row.get("baseline_reliability") == "thin_sources"
            ),
        },
        "message": message,
    }


def _shape_sticker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "slot": row.get("slot"),
        "wear": _decimal_text(row.get("wear")),
        "sticker_id": row.get("stickerId") or row.get("sticker_id"),
    }


def _shape_decoded_sticker(
    row: Any, reference_data: CSGOReferenceData | None
) -> dict[str, Any]:
    sticker_id = row.sticker_id
    name = None
    if reference_data is not None:
        ref = reference_data.stickers_by_id.get(str(sticker_id)) or {}
        raw_name = ref.get("market_hash_name") or ref.get("name")
        if raw_name:
            name = str(raw_name).removeprefix("Sticker | ")
    return {
        "name": name,
        "slot": row.slot,
        "wear": _float_text(row.wear),
        "sticker_id": sticker_id,
        "scale": _float_text(row.scale),
        "rotation": _float_text(row.rotation),
        "offset_x": _float_text(row.offset_x),
        "offset_y": _float_text(row.offset_y),
        "offset_z": _float_text(row.offset_z),
    }


def _shape_decoded_keychain(
    row: Any, reference_data: CSGOReferenceData | None
) -> dict[str, Any]:
    keychain_id = row.sticker_id
    name = None
    if reference_data is not None:
        ref = reference_data.keychains_by_id.get(str(keychain_id)) or {}
        raw_name = ref.get("market_hash_name") or ref.get("name")
        if raw_name:
            name = str(raw_name).removeprefix("Charm | ")
    return {
        "name": name,
        "slot": row.slot,
        "wear": _float_text(row.wear),
        "keychain_id": keychain_id,
        "scale": _float_text(row.scale),
        "rotation": _float_text(row.rotation),
        "offset_x": _float_text(row.offset_x),
        "offset_y": _float_text(row.offset_y),
        "offset_z": _float_text(row.offset_z),
    }


def _wear_name(float_value: float) -> str:
    wear = Decimal(str(float_value))
    if wear < Decimal("0.07"):
        return "Factory New"
    if wear < Decimal("0.15"):
        return "Minimal Wear"
    if wear < Decimal("0.38"):
        return "Field-Tested"
    if wear < Decimal("0.45"):
        return "Well-Worn"
    return "Battle-Scarred"


def _is_stattrak_quality(quality: int) -> bool:
    return quality == 9


def _is_souvenir_quality(quality: int) -> bool:
    return quality == 12


def _explanation(*, status: str, market_hash_name: str | None) -> str:
    if status == "ok":
        return (
            f"Exact asset attributes were read from the public inventory. "
            f"The market baseline for {market_hash_name} is a deterministic "
            f"USD range from local market-name data. It does not include "
            f"float, seed, sticker, or charm premiums."
        )
    return (
        f"Exact asset attributes were read for {market_hash_name}, but no "
        f"local USD market rows are available for a market baseline."
    )


def _inspect_explanation(*, status: str, market_hash_name: str) -> str:
    if status == "ok":
        return (
            f"Exact asset attributes were decoded from the inspect link. "
            f"The market baseline for {market_hash_name} is a deterministic "
            f"USD range from local market-name data. It does not include "
            f"float, seed, sticker, or charm premiums."
        )
    return (
        f"Exact asset attributes were decoded for {market_hash_name}, but no "
        f"local USD market rows are available for a market baseline."
    )
