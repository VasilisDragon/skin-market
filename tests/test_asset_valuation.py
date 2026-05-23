"""Public-inventory and inspect-link asset market-baseline tests."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.asset_valuation import (
    PREMIUM_SIGNAL_AVAILABILITY,
    CSGOReferenceData,
    CSGOReferenceUnavailableError,
    InspectLinkUnsupportedError,
    InventoryUnavailableError,
    PricePoint,
    build_asset_evidence,
    build_market_baseline,
    decode_modern_inspect_link,
    fetch_csgo_reference_data,
    fetch_pricempire_inventory,
    find_inventory_asset,
    parse_inventory_item_url,
    parse_inventory_owner_url,
    resolve_decoded_market_hash_name,
    resolve_steam_id,
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
_LIVE_CROSSCHECK_ENV = "RUN_LIVE_ASSET_CROSSCHECKS"
_FORBIDDEN_PREMIUM_FIELDS = {
    "premium_price",
    "premium_value",
    "premium_range",
    "premium_low",
    "premium_mid",
    "premium_high",
    "premium_multiplier",
    "estimated_true_value",
    "estimated_value",
    "appraisal",
}


def _known_answer_cases() -> list[dict]:
    return json.loads(_KNOWN_ANSWERS_PATH.read_text())


def _inspect_known_answer_cases() -> list[dict]:
    return json.loads(_INSPECT_KNOWN_ANSWERS_PATH.read_text())


def _dmarket_object(case: dict) -> dict:
    source = case["source"]
    fixture = json.loads(Path(source["path"]).read_text())
    return fixture["objects"][source["object_index"]]


def _reference_data_for_case(case: dict) -> CSGOReferenceData:
    """Build a minimal fake schema for route passthrough tests.

    This intentionally does not prove CSGO-API schema correctness. The
    network-gated tests below perform that independent cross-check.
    """
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


def _require_live_asset_crosscheck() -> None:
    if os.environ.get(_LIVE_CROSSCHECK_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_CROSSCHECK_ENV}=1 and opt into -m network to run "
            "live asset cross-checks"
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


def _assert_no_premium_price_fields(value) -> None:
    if isinstance(value, dict):
        forbidden = _FORBIDDEN_PREMIUM_FIELDS.intersection(value)
        assert not forbidden
        for child in value.values():
            _assert_no_premium_price_fields(child)
        return
    if isinstance(value, list):
        for child in value:
            _assert_no_premium_price_fields(child)


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


def test_parse_inventory_owner_link_accepts_profile_inventory() -> None:
    ref = parse_inventory_owner_url(
        "https://steamcommunity.com/profiles/76561199276192848/inventory/"
    )
    assert ref.steam_id == "76561199276192848"
    assert ref.vanity_id is None
    assert ref.app_id == "730"
    assert ref.context_id == "2"


def test_parse_inventory_owner_link_accepts_item_fragment() -> None:
    ref = parse_inventory_owner_url(_INVENTORY_URL)
    assert ref.steam_id == "76561199276192848"
    assert ref.app_id == "730"
    assert ref.context_id == "2"


def test_rejects_non_cs2_inventory_fragment() -> None:
    with pytest.raises(ValueError, match="Only CS2"):
        parse_inventory_item_url(
            "https://steamcommunity.com/profiles/76561199276192848/"
            "inventory/#570_2_51590003382"
        )


def test_build_market_baseline_uses_median_min_max() -> None:
    baseline = build_market_baseline(
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

    assert baseline is not None
    assert baseline["low"] == "150.13"
    assert baseline["mid"] == "198.70"
    assert baseline["high"] == "258.18"
    assert baseline["confidence"] == "high"


def test_asset_evidence_flags_low_float_and_doppler_phase() -> None:
    evidence = build_asset_evidence(
        {
            "market_hash_name": "★ StatTrak™ Karambit | Doppler (Factory New) - Ruby",
            "float_value": "0.006441197823733091",
            "paint_seed": 400,
            "paint_id": 415,
            "low_rank": 3,
            "high_rank": 14,
            "stickers": [],
            "charms": [],
        }
    )

    flags = {row["code"]: row for row in evidence["driver_flags"]}
    assert flags["low_float_for_wear_band"]["present"] is True
    assert flags["low_float_for_wear_band"]["category"] == "low"
    assert flags["pattern_sensitive_family"]["present"] is True
    assert flags["pattern_sensitive_family"]["category"] == "doppler"
    assert flags["phase_already_in_market_name"]["present"] is True
    assert flags["phase_already_in_market_name"]["category"] == "ruby"
    assert flags["rank_present"]["present"] is True
    assert evidence["attributes"]["wear_band"]["name"] == "Factory New"
    assert evidence["attributes"]["is_stattrak"] is True
    assert evidence["signal_availability"]["low_float_for_wear_band"] == (
        PREMIUM_SIGNAL_AVAILABILITY["low_float_for_wear_band"]
    )


def test_asset_evidence_flags_applied_stickers_without_price_signal() -> None:
    evidence = build_asset_evidence(
        {
            "market_hash_name": "StatTrak™ M4A4 | Howl (Factory New)",
            "float_value": "0.059376537799835205",
            "paint_seed": 447,
            "paint_id": None,
            "stickers": [
                {
                    "name": "Sticker | Titan (Holo) | Katowice 2014",
                    "slot": 0,
                    "wear": "0",
                    "sticker_id": None,
                }
            ],
            "charms": [],
        }
    )

    flags = {row["code"]: row for row in evidence["driver_flags"]}
    assert flags["applied_stickers"]["present"] is True
    assert flags["applied_stickers"]["category"] == "1_stickers"
    assert flags["low_float_for_wear_band"]["present"] is False
    assert (
        evidence["signal_availability"]["applied_stickers"]["status"]
        == "not_available"
    )
    assert "cannot currently price" in evidence["summary"]
    _assert_no_premium_price_fields(evidence)


def test_asset_evidence_flags_crimson_web_pattern_and_rank() -> None:
    evidence = build_asset_evidence(
        {
            "market_hash_name": "★ StatTrak™ Karambit | Crimson Web (Factory New)",
            "float_value": "0.06860896944999695",
            "paint_seed": 323,
            "low_rank": 1,
            "high_rank": 1,
            "stickers": [],
            "charms": [],
        }
    )

    flags = {row["code"]: row for row in evidence["driver_flags"]}
    assert flags["pattern_sensitive_family"]["present"] is True
    assert flags["pattern_sensitive_family"]["category"] == "crimson_web"
    assert flags["rank_present"]["present"] is True
    assert flags["phase_already_in_market_name"]["present"] is False
    assert (
        evidence["signal_availability"]["pattern_sensitive_family"]["status"]
        == "not_available"
    )


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
    assert body["evidence"] is None


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
    assert body["market_baseline"]["low"] == "150.13"
    assert body["market_baseline"]["mid"] == "174.42"
    assert body["market_baseline"]["high"] == "198.70"
    assert body["evidence"]["attributes"]["wear_band"]["name"] == "Factory New"
    assert body["evidence"]["driver_flags"][1]["code"] == "applied_stickers"
    assert body["evidence"]["driver_flags"][1]["present"] is True
    _assert_no_premium_price_fields(body["evidence"])


def test_inventory_summary_route_returns_portfolio_baseline(client, monkeypatch) -> None:
    inventory = {
        "items": [
            {
                "asset_id": "1",
                "float_value": Decimal("0.035"),
                "paint_seed": 169,
                "stickers": [{"name": "Sticker | Example", "slot": 0}],
                "item": {
                    "market_hash_name": "Souvenir MP9 | Hot Rod (Factory New)",
                    "paint_id": 33,
                },
            },
            {
                "asset_id": "2",
                "float_value": Decimal("0.202"),
                "paint_seed": 712,
                "stickers": [],
                "item": {
                    "market_hash_name": "StatTrak™ M4A1-S | Cyrex (Field-Tested)",
                    "paint_id": 360,
                },
            },
            {
                "asset_id": "3",
                "float_value": Decimal("0.066"),
                "paint_seed": 520,
                "stickers": [],
                "item": {
                    "market_hash_name": "Unpriced Item (Factory New)",
                    "paint_id": 645,
                },
            },
        ]
    }

    monkeypatch.setattr(
        "api.routes.asset_valuation.fetch_pricempire_inventory",
        lambda steam_id, *, force=False: inventory,
    )

    def _price_points(session, name):
        rows = {
            "Souvenir MP9 | Hot Rod (Factory New)": [
                PricePoint("pricempire_a", "pricempire", Decimal("100.00"), 1, None),
                PricePoint("pricempire_b", "pricempire", Decimal("140.00"), 1, None),
            ],
            "StatTrak™ M4A1-S | Cyrex (Field-Tested)": [
                PricePoint("pricempire_a", "pricempire", Decimal("50.00"), 1, None),
                PricePoint("pricempire_b", "pricempire", Decimal("70.00"), 1, None),
            ],
        }
        return rows.get(name, [])

    monkeypatch.setattr(
        "api.routes.asset_valuation.load_latest_usd_price_points",
        _price_points,
    )

    resp = client.post(
        "/asset-valuations/inventory/summary",
        json={"inventory_url": _INVENTORY_URL},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["portfolio_baseline"]["low"] == "150.00"
    assert body["portfolio_baseline"]["mid"] == "180.00"
    assert body["portfolio_baseline"]["high"] == "210.00"
    assert body["portfolio_baseline"]["priced_count"] == 2
    assert body["portfolio_baseline"]["unpriced_count"] == 1
    assert body["portfolio_baseline"]["stickered_count"] == 1
    assert body["portfolio_baseline"]["top_item_share_pct"] == "66.67"
    assert [row["asset_id"] for row in body["top_items"]] == ["1", "2"]
    assert [row["asset_id"] for row in body["largest_spread_items"]] == ["1", "2"]
    assert body["largest_spread_items"][0]["baseline_spread_pct"] == "33.33"
    assert body["unpriced_sample"][0]["market_hash_name"] == "Unpriced Item (Factory New)"
    assert body["evidence"]["attributes"]["total_count"] == 3
    assert body["evidence"]["driver_counts"]["applied_stickers"] == 1
    assert body["top_items"][0]["evidence"]["driver_flags"][1]["present"] is True
    _assert_no_premium_price_fields(body["evidence"])


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
    assert body["evidence"] is None


@pytest.mark.parametrize("case", _inspect_known_answer_cases())
def test_research_inspect_cases_match_local_dmarket_fixture(case) -> None:
    """Transcription guard only; this is not a live correctness proof."""
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
def test_research_inspect_links_decode_offline_against_fixture_values(case) -> None:
    """Offline decoder check against research fixture values.

    Market-name and sticker-name schema resolution is covered by the
    network-gated CSGO-API test below, not by the local fake schema.
    """
    obj = _dmarket_object(case)
    extra = obj["extra"]
    decoded = decode_modern_inspect_link(extra["inspectInGame"])

    assert str(decoded.itemid) == case["expected_asset_id"]
    assert decoded.defindex == case["expected_defindex"]
    assert decoded.quality == case["expected_quality"]
    assert str(decoded.paintwear) == case["expected_float"]
    assert decoded.paintseed == case["expected_paint_seed"]
    assert decoded.paintindex == case["expected_paint_id"]


@pytest.mark.parametrize("case", _inspect_known_answer_cases())
def test_inspect_route_passthrough_fixture_shapes_attributes_and_baseline(
    client, monkeypatch, case
) -> None:
    """Route passthrough/shape test.

    The mocked schema and price rows are derived from the fixture, so this
    test verifies API shaping and baseline math only. It does not prove the
    fixture's exact attributes are correct.
    """
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
    assert body["evidence"]["attributes"]["market_hash_name"] == case[
        "market_hash_name"
    ]
    assert "driver_flags" in body["evidence"]
    _assert_no_premium_price_fields(body["evidence"])

    expected = Decimal(case["expected_value_usd"])
    mid = Decimal(body["market_baseline"]["mid"])
    tolerance_pct = Decimal(case["tolerance_pct"])
    pct_error = abs((mid - expected) / expected * 100)
    assert pct_error <= tolerance_pct


@pytest.mark.parametrize("case", _known_answer_cases())
def test_research_inventory_cases_match_local_dmarket_fixture(case) -> None:
    """Transcription guard only; this is not a live correctness proof."""
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
def test_inventory_route_passthrough_fixture_shapes_attributes_and_baseline(
    client, monkeypatch, case
) -> None:
    """Route passthrough/shape test.

    The mocked Pricempire inventory row is built from fixture expectations, so
    this test verifies response shaping and baseline math only. It does not
    prove Pricempire returns those exact attributes.
    """
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
    assert body["evidence"]["attributes"]["market_hash_name"] == case[
        "market_hash_name"
    ]
    assert "driver_flags" in body["evidence"]
    _assert_no_premium_price_fields(body["evidence"])

    expected = Decimal(case["expected_value_usd"])
    mid = Decimal(body["market_baseline"]["mid"])
    tolerance_pct = Decimal(case["tolerance_pct"])
    pct_error = abs((mid - expected) / expected * 100)
    assert pct_error <= tolerance_pct


@pytest.mark.network
@pytest.mark.parametrize("case", _known_answer_cases())
def test_live_pricempire_inventory_matches_research_fixture(case) -> None:
    """Live Pricempire cross-check against independently recorded fixture values."""
    _require_live_asset_crosscheck()
    if not os.environ.get("PRICEMPIRE_API_KEY"):
        pytest.skip("PRICEMPIRE_API_KEY is required for live inventory checks")

    reference = parse_inventory_item_url(case["inventory_url"])
    steam_id = resolve_steam_id(reference)
    inventory = fetch_pricempire_inventory(steam_id)
    asset = find_inventory_asset(inventory, reference.asset_id)
    item = asset.get("item") or {}

    assert item.get("market_hash_name") == case["market_hash_name"]
    assert str(asset.get("float_value")) == case["expected_float"]
    assert asset.get("paint_seed") == case["expected_paint_seed"]
    assert item.get("paint_id") == case["expected_paint_id"]
    sticker_names = [
        str(row["name"]).removeprefix("Sticker | ")
        for row in asset.get("stickers") or []
    ]
    assert sticker_names == case["expected_stickers"]


@pytest.mark.network
@pytest.mark.parametrize("case", _inspect_known_answer_cases())
def test_live_csgo_schema_resolves_research_fixture_inspect_links(case) -> None:
    """Live CSGO-API schema cross-check for decoded inspect-link attributes."""
    _require_live_asset_crosscheck()
    fetch_csgo_reference_data.cache_clear()
    try:
        reference_data = fetch_csgo_reference_data()
    except CSGOReferenceUnavailableError as exc:
        pytest.skip(str(exc))

    obj = _dmarket_object(case)
    decoded = decode_modern_inspect_link(obj["extra"]["inspectInGame"])
    sticker_names = []
    for sticker in decoded.stickers:
        ref = reference_data.stickers_by_id.get(str(sticker.sticker_id)) or {}
        raw_name = ref.get("market_hash_name") or ref.get("name")
        sticker_names.append(str(raw_name).removeprefix("Sticker | "))

    assert resolve_decoded_market_hash_name(decoded, reference_data) == case[
        "market_hash_name"
    ]
    assert str(decoded.itemid) == case["expected_asset_id"]
    assert decoded.defindex == case["expected_defindex"]
    assert decoded.quality == case["expected_quality"]
    assert str(decoded.paintwear) == case["expected_float"]
    assert decoded.paintseed == case["expected_paint_seed"]
    assert decoded.paintindex == case["expected_paint_id"]
    assert sticker_names == case["expected_stickers"]
