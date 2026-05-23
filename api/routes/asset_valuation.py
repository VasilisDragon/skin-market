"""Public-inventory and inspect-link asset market-baseline routes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.orm import Session

from api.asset_valuation import (
    CSGOReferenceUnavailableError,
    InspectLinkError,
    InspectLinkUnsupportedError,
    InventoryAssetNotFoundError,
    InventoryLinkError,
    InventoryUnavailableError,
    build_inspect_baseline_response,
    build_inventory_baseline_response,
    build_inventory_summary_response,
    decode_modern_inspect_link,
    fetch_csgo_reference_data,
    fetch_pricempire_inventory,
    find_inventory_asset,
    load_latest_usd_price_points,
    parse_inventory_item_url,
    parse_inventory_owner_url,
    resolve_decoded_market_hash_name,
    resolve_steam_id,
    unreadable_inventory_summary_response,
    unreadable_response,
)
from api.schemas import (
    AssetBaselineResponse,
    InspectBaselineRequest,
    InventoryBaselineRequest,
    InventorySummaryRequest,
    InventorySummaryResponse,
)
from db.connection import get_engine

router = APIRouter(tags=["asset market baseline"])


@router.post(
    "/asset-valuations/inventory",
    response_model=AssetBaselineResponse,
)
def inventory_market_baseline(
    request: InventoryBaselineRequest,
) -> AssetBaselineResponse:
    """Return exact attributes plus a market-name baseline for one inventory asset.

    The route returns structured decline states instead of raising 4xx for
    user-input problems so the Discord bot can render a plain explanation.
    Misconfigured server state, such as a missing Pricempire key, still
    fails normally as a 500.
    """
    try:
        reference = parse_inventory_item_url(request.inventory_url)
        steam_id = resolve_steam_id(reference)
        inventory = fetch_pricempire_inventory(steam_id)
        asset = find_inventory_asset(inventory, reference.asset_id)
    except InventoryLinkError as exc:
        return AssetBaselineResponse.model_validate(
            unreadable_response("invalid_inventory_link", str(exc))
        )
    except InventoryAssetNotFoundError as exc:
        return AssetBaselineResponse.model_validate(
            unreadable_response("asset_not_found", str(exc))
        )
    except InventoryUnavailableError as exc:
        return AssetBaselineResponse.model_validate(
            unreadable_response("private_or_unavailable", str(exc))
        )

    market_hash_name = (asset.get("item") or {}).get("market_hash_name")
    price_points = []
    if market_hash_name:
        engine = get_engine()
        with Session(engine) as session:
            price_points = load_latest_usd_price_points(
                session, market_hash_name
            )

    return AssetBaselineResponse.model_validate(
        build_inventory_baseline_response(
            reference=reference,
            steam_id=steam_id,
            asset=asset,
            price_points=price_points,
        )
    )


@router.post(
    "/asset-valuations/inventory/summary",
    response_model=InventorySummaryResponse,
)
def inventory_portfolio_market_baseline(
    request: InventorySummaryRequest,
) -> InventorySummaryResponse:
    """Return a public inventory's summed market-name portfolio baseline."""
    try:
        reference = parse_inventory_owner_url(request.inventory_url)
        steam_id = resolve_steam_id(reference)
        inventory = fetch_pricempire_inventory(steam_id)
    except InventoryLinkError as exc:
        return InventorySummaryResponse.model_validate(
            unreadable_inventory_summary_response("invalid_inventory_link", str(exc))
        )
    except InventoryUnavailableError as exc:
        return InventorySummaryResponse.model_validate(
            unreadable_inventory_summary_response("private_or_unavailable", str(exc))
        )

    market_hash_names = sorted(
        {
            str((asset.get("item") or {}).get("market_hash_name"))
            for asset in inventory.get("items", [])
            if (asset.get("item") or {}).get("market_hash_name")
        }
    )
    price_points_by_name = {}
    if market_hash_names:
        engine = get_engine()
        with Session(engine) as session:
            price_points_by_name = {
                name: load_latest_usd_price_points(session, name)
                for name in market_hash_names
            }

    return InventorySummaryResponse.model_validate(
        build_inventory_summary_response(
            reference=reference,
            steam_id=steam_id,
            inventory=inventory,
            price_points_by_name=price_points_by_name,
        )
    )


@router.post(
    "/asset-valuations/inspect",
    response_model=AssetBaselineResponse,
)
def inspect_market_baseline(
    request: InspectBaselineRequest,
) -> AssetBaselineResponse:
    """Return exact attributes plus a market-name baseline for one inspect link."""
    try:
        decoded = decode_modern_inspect_link(request.inspect_url)
    except InspectLinkUnsupportedError as exc:
        return AssetBaselineResponse.model_validate(
            unreadable_response("legacy_inspect_link", str(exc))
        )
    except InspectLinkError as exc:
        return AssetBaselineResponse.model_validate(
            unreadable_response("invalid_inspect_link", str(exc))
        )

    reference_data = None
    market_hash_name = None
    try:
        reference_data = fetch_csgo_reference_data()
        market_hash_name = resolve_decoded_market_hash_name(
            decoded,
            reference_data,
        )
    except CSGOReferenceUnavailableError:
        reference_data = None
        market_hash_name = None

    price_points = []
    if market_hash_name:
        engine = get_engine()
        with Session(engine) as session:
            price_points = load_latest_usd_price_points(
                session, market_hash_name
            )

    return AssetBaselineResponse.model_validate(
        build_inspect_baseline_response(
            inspect_url=request.inspect_url,
            decoded=decoded,
            market_hash_name=market_hash_name,
            reference_data=reference_data,
            price_points=price_points,
        )
    )
