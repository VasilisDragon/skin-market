"""Phase A public-inventory asset valuation tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.asset_valuation import (
    CSGOReferenceData,
    InspectLinkUnsupportedError,
    InventoryUnavailableError,
    PricePoint,
    build_value_gauge,
    decode_modern_inspect_link,
    parse_inventory_item_url,
    resolve_decoded_market_hash_name,
)
from api.main import app
from db.connection import get_engine

_TEST_TOKEN = "test-token-deadbeefcafebabe1234567890"
_INVENTORY_URL = (
    "https://steamcommunity.com/profiles/76561199276192848/"
    "inventory/#730_2_51590003382"
)
_KNOWN_ANSWERS_PATH = Path("tests/fixtures/inventory_known_answers.json")
_INSPECT_KNOWN_ANSWERS_PATH = Path("tests/fixtures/inspect_known_answers.json")
_LEGACY_INSPECT_URL = (
    "steam://rungame/730/76561202255233023/"
    "+csgo_econ_action_preview%20"
    "S76561199272523861A36450856127D12136724830466029386"
)


def _known_answer_cases() -> list[dict]:
    return json.loads(_KNOWN_ANSWERS_PATH.read_text())


def _inspect_known_answer_cases() -> list[dict]:
    return json.loads(_INSPECT_KNOWN_ANSWERS_PATH.read_text())


def _dmarket_object(case: dict) -> dict:
    source = case["source"]
    fixture = json.loads(Path(source["path"]).read_text())
    return fixture["objects"][source["object_index"]]


def _reference_data_for_case(case: dict) -> CSGOReferenceData:
    sticker_rows = {
        "2535": {"name": "Sticker | ELEAGUE (Gold) | Boston 2018"},
        "2475": {"name": "Sticker | Cloud9 (Gold) | Boston 2018"},
        "2704": {"name": "Sticker | Skadoodle (Gold) | Boston 2018"},
        "2451": {"name": "Sticker | Virtus.Pro (Gold) | Boston 2018"},
        "4976": {"name": "Sticker | Gambit Gaming (Foil) | Stockholm 2021"},
        "3679": {"name": "Sticker | olofmeister (Foil) | Katowice 2019"},
    }
    wear = (
        "Factory New"
        if "Factory New" in case["market_hash_name"]
        else "Field-Tested"
    )
    return CSGOReferenceData(
        skins_not_grouped=[
            {
                "weapon": {"weapon_id": case["expected_defindex"]},
                "paint_index": str(case["expected_paint_id"]),
                "wear": {"name": wear},
                "stattrak": case["expected_quality"] == 9,
                "souvenir": case["expected_quality"] == 12,
                "market_hash_name": case["market_hash_name"],
            }
        ],
        stickers_by_id=sticker_rows,
        keychains_by_id={},
    )


@pytest.fixture(autouse=True)
def _set_api_token(monkeypatch):
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_engine.cache_clear()
    yield
    get_engine.cache_clear()


@pytest.fixture
def client() -> TestClient:
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
    return c


def test_parse_numeric_profile_inventory_link() -> None:
    ref = parse_inventory_item_url(_INVENTORY_URL)
    assert ref.steam_id == "76561199276192848"
    assert ref.vanity_id is None
    assert ref.app_id == "730"
    assert ref.context_id == "2"
    assert ref.asset_id == "51590003382"


def test_parse_vanity_inventory_link_defers_resolution() -> None:
    ref = parse_inventory_item_url(
        "https://steamcommunity.com/id/some-trader/inventory/#730_2_123"
    )
    assert ref.steam_id is None
    assert ref.vanity_id == "some-trader"
    assert ref.asset_id == "123"


def test_rejects_non_cs2_inventory_fragment() -> None:
    with pytest.raises(ValueError, match="Only CS2"):
        parse_inventory_item_url(
            "https://steamcommunity.com/profiles/76561199276192848/"
            "inventory/#570_2_51590003382"
        )


def test_build_value_gauge_uses_median_min_max() -> None:
    gauge = build_value_gauge(
        [
            PricePoint("skinport", "direct", Decimal("258.18"), 66, None),
            PricePoint("pricempire_buff163", "pricempire", Decimal("198.70"), 247, None),
            PricePoint(
                "pricempire_buff163_buy",
                "pricempire",
                Decimal("150.13"),
                7,
                None,
            ),
        ]
    )

    assert gauge is not None
    assert gauge["low"] == "150.13"
    assert gauge["mid"] == "198.70"
    assert gauge["high"] == "258.18"
    assert gauge["confidence"] == "high"


def test_inventory_route_returns_structured_decline(
    client, monkeypatch
) -> None:
    def _raise_unavailable(steam_id: str, *, force: bool = False):
        raise InventoryUnavailableError("private inventory")

    monkeypatch.setattr(
        "api.routes.asset_valuation.fetch_pricempire_inventory",
        _raise_unavailable,
    )

    resp = client.post(
        "/asset-valuations/inventory",
        json={"inventory_url": _INVENTORY_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unreadable"
    assert body["reason"] == "private_or_unavailable"
    assert "private inventory" in body["message"]


def test_inventory_route_returns_asset_and_gauge(client, monkeypatch) -> None:
    inventory = {
        "items": [
            {
                "asset_id": "51590003382",
                "d": "proof",
                "float_value": Decimal("0.035739749670028687"),
                "paint_seed": 169,
                "low_rank": None,
                "high_rank": None,
                "stickers": [
                    {
                        "name": "Natus Vincere | Stockholm 2021",
                        "slot": 0,
                        "wear": None,
                        "stickerId": 1234,
                    }
                ],
                "charms": None,
                "item": {
                    "market_hash_name": "Souvenir MP9 | Hot Rod (Factory New)",
                    "paint_id": 33,
                },
            }
        ]
    }

    monkeypatch.setattr(
        "api.routes.asset_valuation.fetch_pricempire_inventory",
        lambda steam_id, *, force=False: inventory,
    )
    monkeypatch.setattr(
        "api.routes.asset_valuation.load_latest_usd_price_points",
        lambda session, name: [
            PricePoint(
                "pricempire_buff163",
                "pricempire",
                Decimal("198.70"),
                247,
                "2026-05-23T08:03:41+00:00",
            ),
            PricePoint(
                "pricempire_buff163_buy",
                "pricempire",
                Decimal("150.13"),
                7,
                "2026-05-23T08:03:41+00:00",
            ),
        ],
    )

    resp = client.post(
        "/asset-valuations/inventory",
        json={"inventory_url": _INVENTORY_URL},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["asset"]["asset_id"] == "51590003382"
    assert body["asset"]["float_value"] == "0.035739749670028687"
    assert body["asset"]["paint_seed"] == 169
    assert body["asset"]["paint_id"] == 33
    assert body["asset"]["stickers"][0]["sticker_id"] == 1234
    assert body["value_gauge"]["low"] == "150.13"
    assert body["value_gauge"]["mid"] == "174.42"
    assert body["value_gauge"]["high"] == "198.70"


def test_legacy_inspect_link_is_scope_boundary() -> None:
    with pytest.raises(InspectLinkUnsupportedError, match="Steam account"):
        decode_modern_inspect_link(_LEGACY_INSPECT_URL)


def test_inspect_route_returns_structured_scope_decline(client) -> None:
    resp = client.post(
        "/asset-valuations/inspect",
        json={"inspect_url": _LEGACY_INSPECT_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unreadable"
    assert body["reason"] == "legacy_inspect_link"
    assert "Steam account" in body["message"]


@pytest.mark.parametrize("case", _inspect_known_answer_cases())
def test_known_answer_inspect_cases_are_backed_by_dmarket_fixture(case) -> None:
    obj = _dmarket_object(case)
    extra = obj["extra"]

    assert extra["inspectInGame"].startswith("steam://run/730")
    assert obj["title"] == case["market_hash_name"]
    assert str(extra["floatValue"]) == case["expected_float"]
    assert extra["paintSeed"] == case["expected_paint_seed"]
    assert extra["paintIndex"] == case["expected_paint_id"]
    assert [row["name"] for row in extra.get("stickers") or []] == case[
        "expected_stickers"
    ]
    assert str(Decimal(obj["price"]["USD"]) / Decimal("100")) == case[
        "expected_value_usd"
    ]


@pytest.mark.parametrize("case", _inspect_known_answer_cases())
def test_known_answer_inspect_links_decode_offline(case) -> None:
    obj = _dmarket_object(case)
    extra = obj["extra"]
    decoded = decode_modern_inspect_link(extra["inspectInGame"])
    reference_data = _reference_data_for_case(case)

    assert resolve_decoded_market_hash_name(decoded, reference_data) == case[
        "market_hash_name"
    ]
    assert str(decoded.itemid) == case["expected_asset_id"]
    assert decoded.defindex == case["expected_defindex"]
    assert decoded.quality == case["expected_quality"]
    assert str(decoded.paintwear) == case["expected_float"]
    assert decoded.paintseed == case["expected_paint_seed"]
    assert decoded.paintindex == case["expected_paint_id"]


@pytest.mark.parametrize("case", _inspect_known_answer_cases())
def test_known_answer_inspect_fixture_reproduces_attributes_and_value(
    client, monkeypatch, case
) -> None:
    obj = _dmarket_object(case)
    inspect_url = obj["extra"]["inspectInGame"]

    monkeypatch.setattr(
        "api.routes.asset_valuation.fetch_csgo_reference_data",
        lambda: _reference_data_for_case(case),
    )

    def _price_points(session, name):
        assert name == case["market_hash_name"]
        return [
            PricePoint(
                source=row["source"],
                source_family=row["source_family"],
                price=Decimal(row["price"]),
                volume=row["volume"],
                observed_at="2026-05-23T00:00:00+00:00",
            )
            for row in case["price_points"]
        ]

    monkeypatch.setattr(
        "api.routes.asset_valuation.load_latest_usd_price_points",
        _price_points,
    )

    resp = client.post(
        "/asset-valuations/inspect",
        json={"inspect_url": inspect_url},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["asset"]["asset_id"] == case["expected_asset_id"]
    assert body["asset"]["market_hash_name"] == case["market_hash_name"]
    assert body["asset"]["float_value"] == case["expected_float"]
    assert body["asset"]["paint_seed"] == case["expected_paint_seed"]
    assert body["asset"]["paint_id"] == case["expected_paint_id"]
    assert body["asset"]["defindex"] == case["expected_defindex"]
    assert [row["name"] for row in body["asset"]["stickers"]] == case[
        "expected_stickers"
    ]

    expected = Decimal(case["expected_value_usd"])
    mid = Decimal(body["value_gauge"]["mid"])
    tolerance_pct = Decimal(case["tolerance_pct"])
    pct_error = abs((mid - expected) / expected * 100)
    assert pct_error <= tolerance_pct


@pytest.mark.parametrize("case", _known_answer_cases())
def test_known_answer_cases_are_backed_by_dmarket_fixture(case) -> None:
    """Guard that the known-answer cases come from independent fixture data."""
    source = case["source"]
    assert source["kind"] == "dmarket_fixture"
    fixture = json.loads(Path(source["path"]).read_text())
    obj = fixture["objects"][source["object_index"]]
    extra = obj["extra"]

    assert extra["viewAtSteam"] == case["inventory_url"]
    assert obj["title"] == case["market_hash_name"]
    assert str(extra["floatValue"]) == case["expected_float"]
    assert extra["paintSeed"] == case["expected_paint_seed"]
    assert extra["paintIndex"] == case["expected_paint_id"]
    assert [row["name"] for row in extra.get("stickers") or []] == case[
        "expected_stickers"
    ]
    assert str(Decimal(obj["price"]["USD"]) / Decimal("100")) == case[
        "expected_value_usd"
    ]


@pytest.mark.parametrize("case", _known_answer_cases())
def test_known_answer_inventory_fixture_reproduces_attributes_and_value(
    client, monkeypatch, case
) -> None:
    """Fixture gate: exact asset attributes and value tolerance."""
    ref = parse_inventory_item_url(case["inventory_url"])
    inventory = {
        "items": [
            {
                "asset_id": ref.asset_id,
                "d": "known-answer-proof",
                "float_value": Decimal(case["expected_float"]),
                "paint_seed": case["expected_paint_seed"],
                "low_rank": None,
                "high_rank": None,
                "stickers": [
                    {"name": name, "slot": slot, "wear": None, "stickerId": 10_000 + slot}
                    for slot, name in enumerate(case["expected_stickers"])
                ],
                "charms": None,
                "item": {
                    "market_hash_name": case["market_hash_name"],
                    "paint_id": case["expected_paint_id"],
                },
            }
        ]
    }

    monkeypatch.setattr(
        "api.routes.asset_valuation.fetch_pricempire_inventory",
        lambda steam_id, *, force=False: inventory,
    )

    def _price_points(session, name):
        assert name == case["market_hash_name"]
        return [
            PricePoint(
                source=row["source"],
                source_family=row["source_family"],
                price=Decimal(row["price"]),
                volume=row["volume"],
                observed_at="2026-05-23T00:00:00+00:00",
            )
            for row in case["price_points"]
        ]

    monkeypatch.setattr(
        "api.routes.asset_valuation.load_latest_usd_price_points",
        _price_points,
    )

    resp = client.post(
        "/asset-valuations/inventory",
        json={"inventory_url": case["inventory_url"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["asset"]["market_hash_name"] == case["market_hash_name"]
    assert body["asset"]["float_value"] == case["expected_float"]
    assert body["asset"]["paint_seed"] == case["expected_paint_seed"]
    assert body["asset"]["paint_id"] == case["expected_paint_id"]
    assert [row["name"] for row in body["asset"]["stickers"]] == case[
        "expected_stickers"
    ]

    expected = Decimal(case["expected_value_usd"])
    mid = Decimal(body["value_gauge"]["mid"])
    tolerance_pct = Decimal(case["tolerance_pct"])
    pct_error = abs((mid - expected) / expected * 100)
    assert pct_error <= tolerance_pct
