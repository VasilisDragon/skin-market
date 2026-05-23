"""Public-inventory asset valuation routes."""

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
    build_inspect_valuation_response,
    build_inventory_valuation_response,
    decode_modern_inspect_link,
    fetch_csgo_reference_data,
    fetch_pricempire_inventory,
    find_inventory_asset,
    load_latest_usd_price_points,
    parse_inventory_item_url,
    resolve_decoded_market_hash_name,
    resolve_steam_id,
    unreadable_response,
)
from api.schemas import (
    InspectValuationRequest,
    InventoryValuationRequest,
    InventoryValuationResponse,
)
from db.connection import get_engine

router = APIRouter(tags=["asset valuation"])


@router.post(
    "/asset-valuations/inventory",
    response_model=InventoryValuationResponse,
)
def value_inventory_item(
    request: InventoryValuationRequest,
) -> InventoryValuationResponse:
    """Value one exact asset from a public Steam inventory link.

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
        return InventoryValuationResponse.model_validate(
            unreadable_response("invalid_inventory_link", str(exc))
        )
    except InventoryAssetNotFoundError as exc:
        return InventoryValuationResponse.model_validate(
            unreadable_response("asset_not_found", str(exc))
        )
    except InventoryUnavailableError as exc:
        return InventoryValuationResponse.model_validate(
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

    return InventoryValuationResponse.model_validate(
        build_inventory_valuation_response(
            reference=reference,
            steam_id=steam_id,
            asset=asset,
            price_points=price_points,
        )
    )


@router.post(
    "/asset-valuations/inspect",
    response_model=InventoryValuationResponse,
)
def value_inspect_link(
    request: InspectValuationRequest,
) -> InventoryValuationResponse:
    """Value one exact asset from a modern CS2 inspect link."""
    try:
        decoded = decode_modern_inspect_link(request.inspect_url)
    except InspectLinkUnsupportedError as exc:
        return InventoryValuationResponse.model_validate(
            unreadable_response("legacy_inspect_link", str(exc))
        )
    except InspectLinkError as exc:
        return InventoryValuationResponse.model_validate(
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

    return InventoryValuationResponse.model_validate(
        build_inspect_valuation_response(
            inspect_url=request.inspect_url,
            decoded=decoded,
            market_hash_name=market_hash_name,
            reference_data=reference_data,
            price_points=price_points,
        )
    )
