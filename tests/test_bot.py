"""Bot tests — Phase 7c.

Three test modules in one file:

1. ``TestTools*`` — ported from the Phase 7b ``test_bot_skill.py``
   suite. The wrapper functions in ``bot.tools`` are the same code
   (re-pointed at ``http://api:8000`` for the in-compose path).
   pytest-httpx mocks all HTTP; no network.
2. ``TestDeepSeekClient*`` — the tool-use loop in
   ``bot.deepseek_client``. The chat client is mocked so we don't
   need a live DeepSeek API call. Each test
   scripts the responses the model would produce, including the
   defensive cases (malformed args, unknown tool, runaway loop).
3. ``TestDiscordRender`` — pure-Python helpers (allowlist parsing,
   mention stripping). discord.py is only imported by
   ``attachment_to_file`` and that path is tested separately.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from bot import deepseek_client, discord_render
from bot.tools import (
    HISTORY_DOWNSAMPLE_THRESHOLD,
    TOOL_DEFINITIONS,
    TOOL_FUNCTIONS,
    WATCHLIST_SAMPLE_SIZE,
    ApiAuthError,
    ApiUnexpectedError,
    ApiUnreachableError,
    Attachment,
    ItemNotInWatchlistError,
    _refresh_items_cache,
    evaluate_deal,
    list_watchlist,
    market_baseline_inspect_link,
    market_baseline_inventory_item,
    market_baseline_inventory_summary,
    narrative_today,
    query_current_price,
    query_drift,
    query_price_history,
    render_chart,
    whats_interesting,
)

_TEST_TOKEN = "test-bot-token-deadbeefcafe"
_BASE = "http://api-test-host:8000"


@pytest.fixture(autouse=True)
def _bot_env(monkeypatch):
    """Pin the env that ``bot.tools._client`` reads."""
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
    monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)


@pytest.fixture(autouse=True)
def _reset_items_cache():
    """Clear the items cache before AND after each test so a test
    that populates it (e.g. substrate-tier wear-disambiguation cases)
    doesn't leak into the next test. Phase 2b Step 9."""
    _refresh_items_cache(None)
    yield
    _refresh_items_cache(None)


def _drift_payload(slug: str = "x", pairs: list[dict] | None = None) -> dict:
    """Stock /items/{slug}/drift response with empty pairs by default.
    Tests exercising drift behavior pass explicit pairs."""
    return {
        "slug": slug,
        "display_name": slug.upper(),
        "tier": "curated",
        "pairs": pairs or [],
    }


# =====================================================================
# Section 1: bot.tools — HTTP wrappers (ported from 7b test_bot_skill)
# =====================================================================


class TestToolsRegistry:
    def test_definitions_and_functions_match(self) -> None:
        names_in_defs = {d["function"]["name"] for d in TOOL_DEFINITIONS}
        assert names_in_defs == set(TOOL_FUNCTIONS.keys())
        assert names_in_defs == {
            "list_watchlist",
            "query_current_price",
            "query_price_history",
            "render_chart",
            "evaluate_deal",
            "market_baseline_inventory_item",
            "market_baseline_inventory_summary",
            "market_baseline_inspect_link",
            "query_drift",
            "narrative_today",
            "whats_interesting",
        }

    def test_every_definition_has_concrete_trigger_examples(self) -> None:
        """Open-source models need explicit trigger phrases in the
        tool description. Guard against drift — every description must
        mention 'Call' or 'Trigger' or 'when'."""
        for d in TOOL_DEFINITIONS:
            desc = d["function"]["description"].lower()
            assert (
                "call this" in desc
                or "call when" in desc
                or "trigger" in desc
            ), f"Tool {d['function']['name']} description lacks trigger guidance"


class TestToolsAuthAndConnectivity:
    def test_missing_token_raises_apiautherror(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("SKIN_MARKET_API_TOKEN", raising=False)
        with pytest.raises(ApiAuthError):
            list_watchlist()

    def test_401_from_api_raises_apiautherror(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items", status_code=401
        )
        with pytest.raises(ApiAuthError):
            list_watchlist()

    def test_network_error_raises_apiunreachable(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_exception(httpx.ConnectError("refused"))
        with pytest.raises(ApiUnreachableError):
            list_watchlist()

    def test_5xx_raises_apiunexpected(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items", status_code=503, text="oops"
        )
        with pytest.raises(ApiUnexpectedError):
            list_watchlist()

    def test_authorization_header_threaded(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{_BASE}/items", json=[])
        list_watchlist()
        request = httpx_mock.get_requests()[0]
        assert (
            request.headers["Authorization"]
            == f"Bearer {_TEST_TOKEN}"
        )

    def test_market_baseline_inventory_item_wraps_api_route(self, httpx_mock) -> None:
        payload = {
            "status": "ok",
            "reason": None,
            "message": "baseline",
            "reference": {"asset_id": "51590003382"},
            "asset": {"float_value": "0.035739749670028687"},
            "market_baseline": {
                "low": "150.13",
                "mid": "174.42",
                "high": "198.70",
            },
            "price_points": [],
        }
        httpx_mock.add_response(
            method="POST",
            url=f"{_BASE}/asset-valuations/inventory",
            json=payload,
        )

        result = market_baseline_inventory_item(
            "https://steamcommunity.com/profiles/76561199276192848/"
            "inventory/#730_2_51590003382"
        )

        assert result == payload
        request = httpx_mock.get_request()
        assert request is not None
        assert json.loads(request.content) == {
            "inventory_url": (
                "https://steamcommunity.com/profiles/76561199276192848/"
                "inventory/#730_2_51590003382"
            )
        }

    def test_market_baseline_inspect_link_wraps_api_route(self, httpx_mock) -> None:
        payload = {
            "status": "ok",
            "reason": None,
            "message": "baseline",
            "reference": {"inspect_link_format": "modern_encoded"},
            "asset": {"float_value": "0.035739749670028687"},
            "market_baseline": {
                "low": "150.13",
                "mid": "174.42",
                "high": "198.70",
            },
            "price_points": [],
        }
        httpx_mock.add_response(
            method="POST",
            url=f"{_BASE}/asset-valuations/inspect",
            json=payload,
        )

        result = market_baseline_inspect_link(
            "steam://run/730//+csgo_econ_action_preview%20A0B016"
        )

        assert result == payload
        request = httpx_mock.get_request()
        assert request is not None
        assert json.loads(request.content) == {
            "inspect_url": "steam://run/730//+csgo_econ_action_preview%20A0B016"
        }

    def test_market_baseline_inventory_summary_wraps_api_route(self, httpx_mock) -> None:
        payload = {
            "status": "ok",
            "reason": None,
            "message": "summary",
            "reference": {"steam_id": "76561199276192848"},
            "portfolio_baseline": {
                "low": "151.12",
                "mid": "175.41",
                "high": "201.07",
                "priced_count": 2,
                "unpriced_count": 1,
                "stickered_count": 1,
                "top_item_share_pct": "68.41",
            },
            "top_items": [],
            "largest_spread_items": [],
            "unpriced_sample": [],
        }
        httpx_mock.add_response(
            method="POST",
            url=f"{_BASE}/asset-valuations/inventory/summary",
            json=payload,
        )

        result = market_baseline_inventory_summary(
            "https://steamcommunity.com/profiles/76561199276192848/inventory/"
        )

        assert result == payload
        request = httpx_mock.get_request()
        assert request is not None
        assert json.loads(request.content) == {
            "inventory_url": (
                "https://steamcommunity.com/profiles/76561199276192848/inventory/"
            )
        }


class TestQueryCurrentPriceComposer:
    """The two-state composer is the only non-trivial tool. Re-test the
    categorization, the anomaly flag, and the never_observed fallback
    so a refactor can't quietly break the bot's rendering. (The
    three-state composer's "unavailable" state collapsed to
    never_observed in Phase 2c with the item_unavailability_streak
    removal.)"""

    @staticmethod
    def _price(sources: list[dict], tier: str = "curated") -> dict:
        return {
            "slug": "x",
            "display_name": "X",
            "tier": tier,
            "sources": sources,
        }

    @staticmethod
    def _insights(rows: list[dict]) -> dict:
        return {"slug": "x", "tier": "curated", "insights": rows}

    @staticmethod
    def _register_default_drift(httpx_mock) -> None:
        """Most price-composer tests don't exercise drift; default to
        empty pairs so the /drift call the bot now makes for curated-tier
        items doesn't 404. Tests that want non-empty drift register
        their own response BEFORE calling query_current_price."""
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift", json=_drift_payload(),
        )

    def test_fresh_and_never_observed(self, httpx_mock) -> None:
        """Sources with no observation collapse to never_observed.
        Phase 2c collapsed the previous three-state model
        (fresh / unavailable-with-streak / never_observed) to two
        states with the removal of item_unavailability_streak — sources
        the collector has never polled (or hasn't polled in a while)
        all land in never_observed uniformly."""
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json=self._price(
                [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "33.06",
                        "volume": 521,
                        "last_polled_at": (
                            (now - timedelta(minutes=10)).isoformat()
                        ),
                        "last_changed_at": (
                            (now - timedelta(minutes=10)).isoformat()
                        ),
                    }
                ]
            ),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights([]),
        )
        self._register_default_drift(httpx_mock)
        result = query_current_price("x")
        states = {p["source"]: p["state"] for p in result["per_source"]}
        assert states == {
            "skinport": "fresh",
            "steam_market": "never_observed",
            "dmarket": "never_observed",
        }
        # Denomination tagged on never_observed entries too.
        denoms = {
            p["source"]: p["denomination"] for p in result["per_source"]
        }
        assert denoms["dmarket"] == "usd"
        assert denoms["steam_market"] == "wallet_credit"
        assert result["anomaly_flag"] is None

    def test_stale_when_older_than_4h(self, httpx_mock) -> None:
        """`state == stale` is driven by `last_polled_at`, NOT
        `last_changed_at` — a poll older than STALE_HOURS=4h means the
        collector has genuinely lost reach on that source for that item.
        ADR 017 split.
        """
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json=self._price(
                [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "28.00",
                        "volume": 27,
                        "last_polled_at": (
                            (now - timedelta(hours=5)).isoformat()
                        ),
                        "last_changed_at": (
                            (now - timedelta(hours=5)).isoformat()
                        ),
                    }
                ]
            ),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights([]),
        )
        self._register_default_drift(httpx_mock)
        result = query_current_price("x")
        sk = next(p for p in result["per_source"] if p["source"] == "skinport")
        assert sk["state"] == "stale"

    def test_polled_fresh_but_price_flat_is_fresh_not_stale(
        self, httpx_mock
    ) -> None:
        """The Phase 1 fix in a sentence: an item polled cleanly 2
        minutes ago but whose price hasn't moved in 16h is FRESH, not
        stale. The 16h gap surfaces as ``price_flat_minutes`` — an
        informational hint, not a 🟡 warning.
        """
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json=self._price(
                [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "28.00",
                        "volume": 27,
                        "last_polled_at": (
                            (now - timedelta(minutes=2)).isoformat()
                        ),
                        "last_changed_at": (
                            (now - timedelta(hours=16)).isoformat()
                        ),
                    }
                ]
            ),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights([]),
        )
        self._register_default_drift(httpx_mock)
        result = query_current_price("x")
        sk = next(
            p for p in result["per_source"] if p["source"] == "skinport"
        )
        assert sk["state"] == "fresh"
        assert sk["minutes_since_polled"] < 5
        # 16h - 2min poll lag = ~958 minutes (with a few seconds of
        # real-time slop during the test); allow a small margin below
        # the nominal 16h gap to absorb that.
        assert sk["price_flat_minutes"] >= 950
        # observed_at must not bleed through — the bot only reads
        # the two new fields.
        assert "observed_at" not in sk

    def test_anomaly_flag_recent_divergence(self, httpx_mock) -> None:
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price", json=self._price([])
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights(
                [
                    {
                        "insight_type": "cross_source_divergence",
                        "computed_at": (
                            (now - timedelta(minutes=30)).isoformat()
                        ),
                        "value": "-2.89",
                        "text_value": None,
                        "meta": {
                            "source_a_id": "1",
                            "source_b_id": "27",
                        },
                    }
                ]
            ),
        )
        self._register_default_drift(httpx_mock)
        result = query_current_price("x")
        assert result["anomaly_flag"] is not None
        assert "below" in result["anomaly_flag"]["summary"]


class TestRenderChart:
    def test_returns_attachment(self, httpx_mock) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"body"
        httpx_mock.add_response(
            url=re.compile(rf"^{re.escape(_BASE)}/items/x/chart\?.*"),
            content=png,
            headers={"content-type": "image/png"},
        )
        att = render_chart("x", source="skinport", days=7)
        assert isinstance(att, Attachment)
        assert att.content.startswith(b"\x89PNG")
        assert att.filename == "x-skinport-7d.png"


class TestEvaluateDeal:
    def test_posts_offer(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/deals/evaluate",
            json={
                "slug": "x",
                "display_name": "X",
                "offer": {"amount": "42.50", "currency": "usd"},
                "verdict": "above_market",
                "comparable": [],
                "informational": [],
                "summary": "...",
            },
        )
        result = evaluate_deal("x", amount="42.50", currency="usd")
        assert result["verdict"] == "above_market"

    def test_404_raises_typed(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/deals/evaluate", status_code=404
        )
        with pytest.raises(ItemNotInWatchlistError):
            evaluate_deal("x", amount="1.00", currency="usd")


class TestNarrativeToday:
    def test_404_when_absent(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/insights/narrative/latest", status_code=404
        )
        with pytest.raises(ItemNotInWatchlistError):
            narrative_today()


class TestWhatsInteresting:
    def test_passes_hours_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/insights/anomalies/recent\?hours=12$"
            ),
            json={"since": "", "count": 0, "anomalies": []},
        )
        whats_interesting(hours=12)


class TestQueryPriceHistory:
    def test_threads_source_filter(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/items/x/history\?.*source=skinport.*$"
            ),
            json={
                "slug": "x",
                "source": "skinport",
                "since": "",
                "until": "",
                "limit": 500,
                "count": 0,
                "observations": [],
            },
        )
        result = query_price_history("x", source="skinport", days=3)
        assert result["source"] == "skinport"


# =====================================================================
# Section 1c: Phase 2b Step 9 — query_drift + tier-aware shaping
# =====================================================================


def _items_fixture_for_orphan_test() -> list[dict]:
    """A tiny items-cache fixture that includes two USP-S Neo-Noir
    wear variants — Field-Tested as deep (the active wear post-Step
    7.1) and Factory New as orphan (the dropped wear). Used to test
    sibling-wear resolution without hitting the API."""
    return [
        {
            "slug": "usp-s-neo-noir-field-tested",
            "market_hash_name": "USP-S | Neo-Noir (Field-Tested)",
            "display_name": "USP-S | Neo-Noir (Field-Tested)",
            "tier": "curated",
            "weapon_name": "USP-S",
            "skin_name": "Neo-Noir",
            "is_stattrak": False,
            "is_souvenir": False,
        },
        {
            "slug": "usp-s-neo-noir-factory-new",
            "market_hash_name": "USP-S | Neo-Noir (Factory New)",
            "display_name": "USP-S | Neo-Noir (Factory New)",
            "tier": "substrate",
            "weapon_name": "USP-S",
            "skin_name": "Neo-Noir",
            "is_stattrak": False,
            "is_souvenir": False,
        },
        {
            "slug": "awp-dragon-lore-factory-new",
            "market_hash_name": "AWP | Dragon Lore (Factory New)",
            "display_name": "AWP | Dragon Lore (Factory New)",
            "tier": "curated",
            "weapon_name": "AWP",
            "skin_name": "Dragon Lore",
            "is_stattrak": False,
            "is_souvenir": False,
        },
        {
            "slug": "awp-dragon-lore-field-tested",
            "market_hash_name": "AWP | Dragon Lore (Field-Tested)",
            "display_name": "AWP | Dragon Lore (Field-Tested)",
            "tier": "substrate",
            "weapon_name": "AWP",
            "skin_name": "Dragon Lore",
            "is_stattrak": False,
            "is_souvenir": False,
        },
    ]


def _drift_pair(
    *,
    verdict: str,
    source_a: str = "skinport",
    source_b: str = "pricempire_skinport",
    drift: str | None = "-0.0123",
    classification: str = "pattern_agnostic",
    curated_price: str | None = "100.00",
    pricempire_price: str | None = "98.00",
    curated_age_min: float | None = 2.0,
    pricempire_age_min: float | None = 1.0,
) -> dict:
    """Build a single DriftPairVerdict-shaped dict for /drift mocks.
    Mirrors api/schemas.py's DriftPairVerdict — tests use this so a
    schema change ripples through one place."""
    return {
        "source_a": source_a,
        "source_b": source_b,
        "verdict": verdict,
        "drift": drift,
        "threshold_used": "0.10",
        "classification": classification,
        "threshold_multiplier": 1.0,
        "computed_at": "2026-05-17T01:30:00Z",
        "curated_price": curated_price,
        "pricempire_price": pricempire_price,
        "curated_last_polled_at": "2026-05-17T01:28:00Z",
        "pricempire_last_polled_at": "2026-05-17T01:29:00Z",
        "curated_age_min": curated_age_min,
        "pricempire_age_min": pricempire_age_min,
        "note": None,
    }


class TestQueryDrift:
    """Direct /drift tool. Mocks /items/{slug}/drift and asserts the
    bot's shaping layer renders the framing strings per verdict."""

    def test_returns_curated_tier_with_pairs(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [
                    _drift_pair(verdict="no_drift", drift="-0.0123"),
                    _drift_pair(
                        verdict="drift_alert",
                        drift="0.1532",
                        source_a="dmarket",
                        source_b="pricempire_dmarket",
                    ),
                ],
            },
        )
        result = query_drift("x")
        assert result["tier"] == "curated"
        assert len(result["pairs"]) == 2
        verdicts = {p["verdict"] for p in result["pairs"]}
        assert verdicts == {"no_drift", "drift_alert"}
        # tier_note and active_wear_hint should NOT appear for deep tier.
        assert "tier_note" not in result
        assert "active_wear_hint" not in result

    def test_drift_alert_formats_signed_pct(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [_drift_pair(verdict="drift_alert", drift="0.1532")],
            },
        )
        result = query_drift("x")
        p = result["pairs"][0]
        assert p["drift_pct"] == "+15.3%"
        assert "+15.3%" in p["framing"]
        assert "10.0%" in p["framing"]  # threshold reference
        assert p["stale_side"] is None

    def test_no_drift_calm_framing(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [_drift_pair(verdict="no_drift", drift="-0.0123")],
            },
        )
        result = query_drift("x")
        p = result["pairs"][0]
        assert p["drift_pct"] == "-1.2%"
        assert "within tolerance" in p["framing"]

    def test_pattern_skip_no_drift_number(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [
                    _drift_pair(
                        verdict="pattern_skip",
                        drift=None,
                        classification="phase_based",
                    )
                ],
            },
        )
        result = query_drift("x")
        p = result["pairs"][0]
        assert p["drift_pct"] is None
        assert "skipped" in p["framing"].lower()
        # The framing must NOT contain a percentage number — pattern_skip
        # is structurally drift-less.
        assert "%" not in p["framing"]

    @pytest.mark.parametrize(
        "verdict,expected_stale_side",
        [
            ("stale_curated", "curated"),
            ("stale_pricempire", "pricempire"),
            ("stale_both", "both"),
        ],
    )
    def test_stale_variants_carry_stale_side(
        self, httpx_mock, verdict: str, expected_stale_side: str
    ) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [
                    _drift_pair(
                        verdict=verdict,
                        drift=None,
                        curated_age_min=120.0,
                        pricempire_age_min=120.0,
                    )
                ],
            },
        )
        result = query_drift("x")
        p = result["pairs"][0]
        assert p["stale_side"] == expected_stale_side
        assert p["drift_pct"] is None

    def test_no_comparable_data_warming_up_framing(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [
                    _drift_pair(
                        verdict="no_comparable_data",
                        drift=None,
                        curated_price=None,
                        pricempire_price=None,
                    )
                ],
            },
        )
        result = query_drift("x")
        p = result["pairs"][0]
        assert p["drift_pct"] is None
        assert "warming up" in p["framing"].lower()

    def test_featured_tier_carries_tier_note_no_pairs(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "featured",
                "pairs": [],
            },
        )
        result = query_drift("x")
        assert result["tier"] == "featured"
        assert result["pairs"] == []
        assert "featured watchlist" in result["tier_note"]
        # No active_wear_hint for broad tier — broad isn't a wear-tier
        # swap case.
        assert "active_wear_hint" not in result

    def test_substrate_tier_with_active_wear_hint(
        self, httpx_mock
    ) -> None:
        """Two real Step 7.1 wear swaps: USP-S Neo-Noir FN (orphan)
        should resolve to FT (deep) as the active sibling."""
        _refresh_items_cache(_items_fixture_for_orphan_test())
        httpx_mock.add_response(
            url=f"{_BASE}/items/usp-s-neo-noir-factory-new/drift",
            json={
                "slug": "usp-s-neo-noir-factory-new",
                "display_name": "USP-S | Neo-Noir (Factory New)",
                "tier": "substrate",
                "pairs": [],
            },
        )
        result = query_drift("usp-s-neo-noir-factory-new")
        assert result["tier"] == "substrate"
        assert result["pairs"] == []
        assert "actively-tracked watchlist" in result["tier_note"]
        assert (
            result["active_wear_hint"]["slug"]
            == "usp-s-neo-noir-field-tested"
        )
        # The tier_note explicitly references the active wear by name.
        assert "Field-Tested" in result["tier_note"]

    def test_substrate_tier_dragon_lore_swap(self, httpx_mock) -> None:
        """AWP Dragon Lore FT (orphan) → FN (deep) is the other Step
        7.1 wear swap; pin it explicitly so a future YAML edit can't
        silently break the hint."""
        _refresh_items_cache(_items_fixture_for_orphan_test())
        httpx_mock.add_response(
            url=f"{_BASE}/items/awp-dragon-lore-field-tested/drift",
            json={
                "slug": "awp-dragon-lore-field-tested",
                "display_name": "AWP | Dragon Lore (Field-Tested)",
                "tier": "substrate",
                "pairs": [],
            },
        )
        result = query_drift("awp-dragon-lore-field-tested")
        assert (
            result["active_wear_hint"]["slug"]
            == "awp-dragon-lore-factory-new"
        )

    def test_substrate_tier_without_sibling_falls_back_gracefully(
        self, httpx_mock
    ) -> None:
        """An orphan with no curated-tier sibling (e.g. a unique skin
        whose only wear was dropped) gets tier_note without an
        active_wear_hint."""
        cache = [
            {
                "slug": "lonely-orphan-field-tested",
                "market_hash_name": "Lonely | Orphan (Field-Tested)",
                "display_name": "Lonely | Orphan (Field-Tested)",
                "tier": "substrate",
                "weapon_name": "Lonely",
                "skin_name": "Orphan",
                "is_stattrak": False,
                "is_souvenir": False,
            }
        ]
        _refresh_items_cache(cache)
        httpx_mock.add_response(
            url=f"{_BASE}/items/lonely-orphan-field-tested/drift",
            json={
                "slug": "lonely-orphan-field-tested",
                "display_name": "Lonely | Orphan (Field-Tested)",
                "tier": "substrate",
                "pairs": [],
            },
        )
        result = query_drift("lonely-orphan-field-tested")
        assert "active_wear_hint" not in result
        assert "actively-tracked watchlist" in result["tier_note"]


class TestQueryCurrentPriceDriftIntegration:
    """The extended query_current_price now fetches /drift in
    addition to /price + /insights for curated-tier items, and merges
    the drift_summary block into the response. Pin the integration."""

    def test_deep_tier_includes_drift_summary(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "sources": [],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json={"slug": "x", "tier": "curated", "insights": []},
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "pairs": [_drift_pair(verdict="no_drift", drift="0.0050")],
            },
        )
        result = query_current_price("x")
        assert result["tier"] == "curated"
        assert "drift_summary" in result
        assert len(result["drift_summary"]["pairs"]) == 1
        assert result["drift_summary"]["pairs"][0]["verdict"] == "no_drift"
        assert "tier_note" not in result

    def test_broad_tier_skips_drift_fetch(self, httpx_mock) -> None:
        """Broad tier doesn't call /drift; pin by NOT registering a
        /drift mock — pytest_httpx would fail if the call were made."""
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "featured",
                "sources": [],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json={"slug": "x", "tier": "featured", "insights": []},
        )
        result = query_current_price("x")
        assert result["tier"] == "featured"
        assert "drift_summary" not in result
        assert "tier_note" in result
        assert "featured watchlist" in result["tier_note"]

    def test_substrate_tier_skips_drift_with_active_wear_hint(
        self, httpx_mock
    ) -> None:
        _refresh_items_cache(_items_fixture_for_orphan_test())
        httpx_mock.add_response(
            url=f"{_BASE}/items/usp-s-neo-noir-factory-new/price",
            json={
                "slug": "usp-s-neo-noir-factory-new",
                "display_name": "USP-S | Neo-Noir (Factory New)",
                "tier": "substrate",
                "sources": [],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/usp-s-neo-noir-factory-new/insights",
            json={
                "slug": "usp-s-neo-noir-factory-new",
                "tier": "substrate",
                "insights": [],
            },
        )
        result = query_current_price("usp-s-neo-noir-factory-new")
        assert result["tier"] == "substrate"
        assert "drift_summary" not in result
        assert (
            result["active_wear_hint"]["slug"]
            == "usp-s-neo-noir-field-tested"
        )

    def test_pricempire_pair_filtered_from_anomaly_flag(
        self, httpx_mock
    ) -> None:
        """Coexistence rule: a cross_source_divergence row whose meta
        names a Pricempire sub-provider is suppressed from
        anomaly_flag — drift_summary owns that pair. Today this is
        empty by construction; the filter is defense-in-depth."""
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "sources": [],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json={
                "slug": "x",
                "tier": "curated",
                "insights": [
                    {
                        "insight_type": "cross_source_divergence",
                        "computed_at": (
                            (now - timedelta(minutes=10)).isoformat()
                        ),
                        "value": "-3.1",
                        "text_value": None,
                        "meta": {
                            "source_a_name": "skinport",
                            "source_b_name": "pricempire_skinport",
                        },
                    },
                    {
                        "insight_type": "cross_source_divergence",
                        "computed_at": (
                            (now - timedelta(minutes=10)).isoformat()
                        ),
                        "value": "-2.5",
                        "text_value": None,
                        "meta": {
                            "source_a_name": "skinport",
                            "source_b_name": "dmarket",
                        },
                    },
                ],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/drift",
            json=_drift_payload(slug="x"),
        )
        result = query_current_price("x")
        # Pricempire-involving divergence suppressed; skinport/dmarket
        # divergence survives.
        flag = result["anomaly_flag"]
        assert flag is not None
        # The "below" framing implies value < 0 was preserved; the
        # surviving row's value is -2.5 (skinport vs dmarket).
        assert flag["z_score"] == "-2.5"


class TestEvaluateDealTierEnvelope:
    """evaluate_deal passes through the API response and now injects
    tier_note + active_wear_hint for broad/orphan tiers."""

    def test_deep_tier_no_envelope_injected(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/deals/evaluate",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "curated",
                "offer": {"amount": "10.00", "currency": "usd"},
                "verdict": "no_comparable_data",
                "comparable": [],
                "informational": [],
                "summary": "n/a",
            },
        )
        result = evaluate_deal("x", "10.00", "usd")
        assert "tier_note" not in result

    def test_broad_tier_gets_tier_note(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/deals/evaluate",
            json={
                "slug": "x",
                "display_name": "X",
                "tier": "featured",
                "offer": {"amount": "10.00", "currency": "usd"},
                "verdict": "no_comparable_data",
                "comparable": [],
                "informational": [],
                "summary": "n/a",
            },
        )
        result = evaluate_deal("x", "10.00", "usd")
        assert "featured watchlist" in result["tier_note"]


class TestQueryPriceHistoryTierEnvelope:
    def test_substrate_history_carries_tier_note(self, httpx_mock) -> None:
        _refresh_items_cache(_items_fixture_for_orphan_test())
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}"
                r"/items/usp-s-neo-noir-factory-new/history.*$"
            ),
            json={
                "slug": "usp-s-neo-noir-factory-new",
                "tier": "substrate",
                "source": None,
                "since": "",
                "until": "",
                "limit": 500,
                "count": 0,
                "observations": [],
            },
        )
        result = query_price_history("usp-s-neo-noir-factory-new")
        assert result.get("tier") == "substrate"
        assert "actively-tracked watchlist" in result["tier_note"]


class TestFindActiveWearMatching:
    """Direct unit coverage of the sibling-wear matcher. The
    integration tests above exercise the same path through
    query_drift / query_current_price; these tests pin the matching
    rules in isolation so a future schema change can't silently
    break the resolution."""

    def test_match_requires_same_stattrak_flag(self) -> None:
        from bot.tools import _find_active_wear

        cache = [
            {
                "slug": "rifle-skin-fn-stt",
                "market_hash_name": "StatTrak™ Rifle | Skin (FN)",
                "tier": "substrate",
                "weapon_name": "Rifle",
                "skin_name": "Skin",
                "is_stattrak": True,
                "is_souvenir": False,
            },
            # Same weapon+skin but DIFFERENT StatTrak flag — should NOT
            # match (StatTrak™ is a separate item from the base).
            {
                "slug": "rifle-skin-ft-base",
                "market_hash_name": "Rifle | Skin (FT)",
                "tier": "curated",
                "weapon_name": "Rifle",
                "skin_name": "Skin",
                "is_stattrak": False,
                "is_souvenir": False,
            },
        ]
        _refresh_items_cache(cache)
        assert _find_active_wear("rifle-skin-fn-stt") is None

    def test_unknown_slug_returns_none(self) -> None:
        from bot.tools import _find_active_wear

        _refresh_items_cache(_items_fixture_for_orphan_test())
        assert _find_active_wear("not-in-cache-slug") is None


class TestSystemPromptPhase2bStep9:
    """Pin the system-prompt additions so a future edit can't quietly
    drop the wear-disambiguation or coexistence rules the LLM relies
    on."""

    def test_prompt_mentions_query_drift_section(self) -> None:
        from bot.system_prompt import SYSTEM_PROMPT

        assert "## query_drift" in SYSTEM_PROMPT
        assert "is X drifting" in SYSTEM_PROMPT

    def test_prompt_requires_wear_clarification(self) -> None:
        from bot.system_prompt import SYSTEM_PROMPT

        assert "ask which wear before using tools" in SYSTEM_PROMPT
        assert "Do not query multiple wears" in SYSTEM_PROMPT
        assert "Do not list item-specific wear availability" in SYSTEM_PROMPT

    def test_prompt_mentions_correctly_priced_clarification(self) -> None:
        from bot.system_prompt import SYSTEM_PROMPT

        assert "correctly priced" in SYSTEM_PROMPT
        assert "clarifying question" in SYSTEM_PROMPT.lower()

    def test_prompt_pins_drift_before_anomaly_render_order(self) -> None:
        from bot.system_prompt import SYSTEM_PROMPT

        # Drift framing must appear BEFORE the anomaly_flag line in
        # the prompt — the LLM reads top-down and the order pins the
        # render-time precedence.
        drift_idx = SYSTEM_PROMPT.find("drift_summary.pairs[].framing")
        anomaly_idx = SYSTEM_PROMPT.find(
            "Cross-source spread anomaly active"
        )
        assert 0 <= drift_idx < anomaly_idx, (
            "drift framing reference must precede the anomaly_flag "
            "reference so render order is unambiguous"
        )

    def test_prompt_limits_asset_baseline_rendering(self) -> None:
        from bot.system_prompt import SYSTEM_PROMPT

        assert "market_baseline_inventory_item" in SYSTEM_PROMPT
        assert "market_baseline_inventory_summary" in SYSTEM_PROMPT
        assert "market_baseline_inspect_link" in SYSTEM_PROMPT
        assert "Do not render a table" in SYSTEM_PROMPT
        assert "name individual sources" in SYSTEM_PROMPT
        assert "Do not add sale predictions" in SYSTEM_PROMPT
        assert "sticker/charm" in SYSTEM_PROMPT
        assert "names exactly" in SYSTEM_PROMPT
        assert "market-name baseline" in SYSTEM_PROMPT
        assert "float, seed, sticker, or charm premiums" in SYSTEM_PROMPT
        assert "After rendering `market_baseline`, stop" in SYSTEM_PROMPT
        assert "Render `market_baseline` under the heading" in SYSTEM_PROMPT
        assert "market baseline section must be the final section" in SYSTEM_PROMPT
        assert "portfolio_baseline" in SYSTEM_PROMPT
        assert "priced/unpriced counts" in SYSTEM_PROMPT
        assert "stickered count" in SYSTEM_PROMPT
        assert "largest_spread_items" in SYSTEM_PROMPT
        assert "Do not mention CSFloat/Skinport/DMarket" in SYSTEM_PROMPT
        assert "do not fall back to market_hash_name averages" in SYSTEM_PROMPT


# =====================================================================
# Section 1b: Phase 7c-fix — tool-result size discipline
# (ADR 016 §"Tool result size discipline")
# =====================================================================


import json as _json  # noqa: E402 — placed near the size tests


def _serialized_len(obj) -> int:
    """Helper: JSON-serialize a tool result and return byte count.
    Approximates what the DeepSeek tool_result payload looks like."""
    return len(_json.dumps(obj, default=str).encode("utf-8"))


class TestListWatchlistSizeDiscipline:
    def test_returns_summarized_shape_not_raw_list(
        self, httpx_mock
    ) -> None:
        """Original /items returned a 48-item list to the LLM and
        exceeded its rendering latency budget. Tool now returns
        {count, by_category, sample}."""
        raw_48 = [
            {
                "slug": f"item-{i}",
                "market_hash_name": f"AK-47 | Skin{i} (Field-Tested)",
                "display_name": f"AK-47 | Skin{i} (Field-Tested)",
            }
            for i in range(48)
        ]
        httpx_mock.add_response(url=f"{_BASE}/items", json=raw_48)
        result = list_watchlist()

        # Shape contract
        assert set(result.keys()) == {"count", "by_category", "sample"}
        assert result["count"] == 48
        assert len(result["sample"]) == WATCHLIST_SAMPLE_SIZE
        # All 48 are AK-47s in this fixture → categorized as rifle.
        assert result["by_category"] == {"rifle": 48}

        # Size — much smaller than the raw 48-record payload.
        assert _serialized_len(result) < 2000, (
            f"Summarized watchlist should be <2KB; got "
            f"{_serialized_len(result)} bytes"
        )

    def test_category_breakdown_diverse_watchlist(
        self, httpx_mock
    ) -> None:
        diverse = [
            {
                "slug": "ak", "market_hash_name": "AK-47 | Redline (FT)",
                "display_name": "AK-47 | Redline (FT)",
            },
            {
                "slug": "awp", "market_hash_name": "AWP | Asiimov (FT)",
                "display_name": "AWP | Asiimov (FT)",
            },
            {
                "slug": "kn", "market_hash_name": "★ Karambit | Doppler (FN)",
                "display_name": "★ Karambit | Doppler (FN)",
            },
            {
                "slug": "gl", "market_hash_name": "★ Sport Gloves | Vice (FT)",
                "display_name": "★ Sport Gloves | Vice (FT)",
            },
            {
                "slug": "dg",
                "market_hash_name": "Desert Eagle | Blaze (FN)",
                "display_name": "Desert Eagle | Blaze (FN)",
            },
        ]
        httpx_mock.add_response(url=f"{_BASE}/items", json=diverse)
        result = list_watchlist()
        # Gloves must check before knife (else ★ Sport Gloves
        # lands in knife). Regression-guards the ordering in
        # _CATEGORY_PATTERNS.
        assert result["by_category"] == {
            "rifle": 1,
            "sniper": 1,
            "knife": 1,
            "gloves": 1,
            "pistol": 1,
        }


class TestQueryPriceHistorySizeDiscipline:
    def _obs(self, source: str, ts: str, price: str) -> dict:
        return {
            "timestamp": ts,
            "source": source,
            "denomination": "usd"
            if source != "steam_market"
            else "wallet_credit",
            "price": price,
            "volume": 10,
        }

    def test_passes_through_when_below_threshold(
        self, httpx_mock
    ) -> None:
        rows = [
            self._obs("skinport", f"2026-05-{i:02d}T00:00:00Z", "30.00")
            for i in range(1, 6)  # 5 rows, well below the threshold
        ]
        httpx_mock.add_response(
            url=re.compile(rf"^{re.escape(_BASE)}/items/x/history\?.*"),
            json={
                "slug": "x",
                "source": "skinport",
                "since": "...",
                "until": "...",
                "limit": 500,
                "count": 5,
                "observations": rows,
            },
        )
        result = query_price_history("x")
        # Raw shape preserved.
        assert "observations" in result
        assert len(result["observations"]) == 5
        assert "downsampled" not in result

    def test_downsamples_when_above_threshold(
        self, httpx_mock
    ) -> None:
        """Above HISTORY_DOWNSAMPLE_THRESHOLD rows → return per-source
        aggregate stats instead of the raw list. Bounded payload for
        LLM rendering."""
        # 50 rows across 2 sources, prices ramping so first/last/min/
        # max are distinct and the test can verify them.
        rows = []
        for i in range(25):
            rows.append(
                self._obs(
                    "skinport",
                    f"2026-05-01T{i:02d}:00:00Z",
                    f"{30 + i}.00",
                )
            )
        for i in range(25):
            rows.append(
                self._obs(
                    "dmarket",
                    f"2026-05-01T{i:02d}:00:00Z",
                    f"{40 + i}.00",
                )
            )
        assert len(rows) > HISTORY_DOWNSAMPLE_THRESHOLD

        httpx_mock.add_response(
            url=re.compile(rf"^{re.escape(_BASE)}/items/x/history\?.*"),
            json={
                "slug": "x",
                "source": None,
                "since": "...",
                "until": "...",
                "limit": 500,
                "count": len(rows),
                "observations": rows,
            },
        )
        result = query_price_history("x", days=30)

        assert result.get("downsampled") is True
        assert "observations" not in result
        assert "per_source_stats" in result
        # 2 sources represented.
        assert set(result["per_source_stats"]) == {"skinport", "dmarket"}
        # First/last/min/max correct for each.
        sk = result["per_source_stats"]["skinport"]
        assert sk["first_price"] == "30.00"
        assert sk["last_price"] == "54.00"
        assert sk["min_price"] == "30.00"
        assert sk["max_price"] == "54.00"
        assert sk["count"] == 25
        # Bounded.
        assert _serialized_len(result) < 3000


class TestWhatsInterestingSizeDiscipline:
    def test_top_n_when_above_threshold(self, httpx_mock) -> None:
        # 25 anomalies — well above the threshold of 10. z-scores
        # ramped so we can verify the top-N picks the largest |z|.
        anomalies = []
        for i in range(25):
            z = (-1 if i % 2 == 0 else 1) * (0.1 * (i + 1))
            anomalies.append(
                {
                    "insight_type": "cross_source_divergence",
                    "slug": f"item-{i}",
                    "display_name": f"Item {i}",
                    "computed_at": "2026-05-13T00:00:00Z",
                    "z_score": f"{z:.2f}",
                    "meta": {},
                }
            )
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/insights/anomalies/recent\?.*"
            ),
            json={
                "since": "...", "count": 25, "anomalies": anomalies
            },
        )
        result = whats_interesting()
        assert result.get("downsampled") is True
        assert result["total_count"] == 25
        assert len(result["anomalies"]) == 10
        # Top 10 by |z| → the highest-index items (largest |z|).
        top_slugs = {a["slug"] for a in result["anomalies"]}
        assert "item-24" in top_slugs  # |z|=2.5
        assert "item-0" not in top_slugs  # |z|=0.1

    def test_pass_through_when_below_threshold(
        self, httpx_mock
    ) -> None:
        anomalies = [
            {
                "insight_type": "volume_anomaly",
                "slug": "x",
                "display_name": "X",
                "computed_at": "2026-05-13T00:00:00Z",
                "z_score": "2.0",
                "meta": {},
            }
        ]
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/insights/anomalies/recent\?.*"
            ),
            json={"since": "...", "count": 1, "anomalies": anomalies},
        )
        result = whats_interesting()
        assert "downsampled" not in result
        assert len(result["anomalies"]) == 1


class TestNarrativeMetaTrimmed:
    def test_meta_collapsed_to_compact_shape(self, httpx_mock) -> None:
        bulky_meta = {
            "as_of": "2026-05-13T03:00:00Z",
            "top_movers": [{"name": f"Item {i}"} for i in range(20)],
            "volume_anomalies": [{"name": "X"} for _ in range(5)],
            "cross_source_divergences": [{"name": "Y"} for _ in range(3)],
        }
        httpx_mock.add_response(
            url=f"{_BASE}/insights/narrative/latest",
            json={
                "computed_at": "2026-05-13T03:00:00Z",
                "text": "Today, X moved up...",
                "meta": bulky_meta,
            },
        )
        result = narrative_today()
        # Text passes through.
        assert result["text"].startswith("Today")
        # Meta is compact.
        assert result["meta"] == {
            "as_of": "2026-05-13T03:00:00Z",
            "cited_count": 28,
        }


class TestEndToEndWhatDoYouTrackPayloadBounded:
    """The original failure mode: 'what items do you track?' fed the
    full 48-item list to the LLM, which timed out rendering it. With
    size discipline applied, the tool_result the LLM sees must be
    bounded (<2KB). This regression-guards the production path."""

    async def test_tool_result_to_llm_is_bounded(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)

        # 48-item API response — what blew up in live testing.
        raw_48 = [
            {
                "slug": f"item-{i}",
                "market_hash_name": f"AK-47 | Skin{i} (Field-Tested)",
                "display_name": f"AK-47 | Skin{i} (Field-Tested)",
            }
            for i in range(48)
        ]
        httpx_mock.add_response(url=f"{_BASE}/items", json=raw_48)

        client = AsyncMock()
        client.chat.side_effect = [
            {
                "message": _make_msg(
                    tool_calls=[
                        {
                            "function": {
                                "name": "list_watchlist",
                                "arguments": {},
                            }
                        }
                    ]
                )
            },
            {
                "message": _make_msg(
                    content="We track 48 items, all rifles."
                )
            },
        ]

        await deepseek_client.handle_user_message(
            "what items do you track?", client=client
        )

        # Inspect the SECOND chat call — its `messages` arg has the
        # tool_result message we just produced.
        second_call_kwargs = client.chat.call_args_list[1].kwargs
        messages = second_call_kwargs["messages"]
        tool_message = next(
            m for m in messages if m.get("role") == "tool"
        )
        # The tool_result content (a JSON string of the summarized
        # watchlist) must be bounded — the pre-fix payload at this
        # site was ~7KB and exceeded DeepSeek's rendering budget.
        assert len(tool_message["content"]) < 2000, (
            f"tool_result fed to DeepSeek is "
            f"{len(tool_message['content'])} bytes; size discipline "
            f"should bound it under 2KB."
        )


# =====================================================================
# Section 2: bot.deepseek_client — tool-use loop
# =====================================================================


def _make_msg(content="", tool_calls=None):
    """Mimic the OpenAI-format message dict DeepSeek returns."""
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _make_tool_call(name: str, arguments: dict | None = None) -> dict:
    return {"function": {"name": name, "arguments": arguments or {}}}


def _scripted_client(responses: list[dict]):
    """Build a mock AsyncClient whose ``chat`` returns each response
    in order."""
    client = AsyncMock()
    client.chat.side_effect = [{"message": r} for r in responses]
    return client


def _tool_messages_from_last_chat_call(client) -> list[dict]:
    messages = client.chat.call_args_list[-1].kwargs["messages"]
    return [m for m in messages if m.get("role") == "tool"]


def _tool_json(content: str) -> dict:
    return json.loads(content)


class TestDeepSeekProviderClient:
    async def test_posts_non_thinking_tool_request_and_logs_usage(
        self, httpx_mock
    ) -> None:
        usage_rows: list[dict] = []
        httpx_mock.add_response(
            url="https://deepseek.test/chat/completions",
            json={
                "id": "chatcmpl-test",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "list_watchlist",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "total_tokens": 24,
                    "prompt_cache_hit_tokens": 10,
                    "prompt_cache_miss_tokens": 10,
                },
            },
        )
        client = deepseek_client.DeepSeekChatClient(
            api_key="test-key",
            base_url="https://deepseek.test",
            discord_user_id="1234",
            usage_logger=lambda **kwargs: usage_rows.append(kwargs),
        )
        response = await client.chat(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "what do you track?"}],
            tools=TOOL_DEFINITIONS,
        )

        request = httpx_mock.get_requests()[0]
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert body["thinking"] == {"type": "disabled"}
        assert body["temperature"] == 0
        assert body["tools"] == TOOL_DEFINITIONS
        assert response["message"]["tool_calls"][0]["id"] == "call_1"
        assert usage_rows[0]["model"] == "deepseek-v4-flash"
        assert usage_rows[0]["discord_user_id"] == "1234"


class TestToolCallingRegressionFixture:
    """Representative user-query → tool-selection fixtures.

    This is the offline guardrail for the LLM backend swap: each case
    fixes the first tool(s) the model must select for a Discord query
    and the shaped tool_result the bot feeds back into the conversation.
    The model is scripted here so the test is deterministic; optional
    live-provider checks can use the same case names, but this fixture
    must stay green in the normal suite.
    """

    async def test_price_lookup_routes_to_current_price_with_drift_shape(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        now = datetime.now(UTC)
        slug = "ak-47-redline-field-tested"
        httpx_mock.add_response(
            url=f"{_BASE}/items/{slug}/price",
            json={
                "slug": slug,
                "display_name": "AK-47 | Redline (Field-Tested)",
                "tier": "curated",
                "sources": [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "31.25",
                        "volume": 18,
                        "last_polled_at": (now - timedelta(minutes=5)).isoformat(),
                        "last_changed_at": (now - timedelta(minutes=50)).isoformat(),
                    }
                ],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/{slug}/insights",
            json={"slug": slug, "tier": "curated", "insights": []},
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/{slug}/drift",
            json=_drift_payload(
                slug,
                [
                    {
                        "source_a": "skinport",
                        "source_b": "pricempire_skinport",
                        "verdict": "no_drift",
                        "drift": "0.0123",
                        "threshold_used": "0.10",
                        "classification": "pattern_agnostic",
                        "computed_at": now.isoformat(),
                        "curated_price": "31.25",
                        "pricempire_price": "30.87",
                    }
                ],
            ),
        )
        client = _scripted_client(
            [
                _make_msg(tool_calls=[_make_tool_call("query_current_price", {"slug": slug})]),
                _make_msg(content="Skinport is $31.25 USD."),
            ]
        )

        reply = await deepseek_client.handle_user_message(
            "what's the AK Redline FT price?", client=client
        )

        assert "Skinport" in reply.text
        first_call = client.chat.call_args_list[0].kwargs
        assert first_call["messages"][1]["content"] == "what's the AK Redline FT price?"
        assert first_call["tools"] == TOOL_DEFINITIONS
        tool_result = _tool_json(_tool_messages_from_last_chat_call(client)[0]["content"])
        assert tool_result["tier"] == "curated"
        assert tool_result["per_source"][0]["source"] == "skinport"
        assert tool_result["per_source"][0]["state"] == "fresh"
        assert tool_result["per_source"][2]["source"] == "steam_market"
        assert tool_result["drift_summary"]["pairs"][0]["verdict"] == "no_drift"

    async def test_drift_query_routes_to_query_drift_shape(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        slug = "m4a4-buzz-kill-field-tested"
        httpx_mock.add_response(
            url=f"{_BASE}/items/{slug}/drift",
            json=_drift_payload(
                slug,
                [
                    {
                        "source_a": "dmarket",
                        "source_b": "pricempire_dmarket",
                        "verdict": "drift_alert",
                        "drift": "0.1046",
                        "threshold_used": "0.10",
                        "classification": "pattern_agnostic",
                        "computed_at": datetime.now(UTC).isoformat(),
                        "curated_price": "275.00",
                        "pricempire_price": "248.97",
                    }
                ],
            ),
        )
        client = _scripted_client(
            [
                _make_msg(tool_calls=[_make_tool_call("query_drift", {"slug": slug})]),
                _make_msg(content="DMarket is above Pricempire."),
            ]
        )

        await deepseek_client.handle_user_message(
            "is M4A4 Buzz Kill FT drifting from Pricempire?", client=client
        )

        tool_result = _tool_json(_tool_messages_from_last_chat_call(client)[0]["content"])
        assert tool_result["pairs"][0]["verdict"] == "drift_alert"
        assert tool_result["pairs"][0]["drift_pct"] == "+10.5%"
        assert "framing" in tool_result["pairs"][0]

    async def test_history_query_routes_to_history_shape(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        slug = "ak-47-redline-field-tested"
        httpx_mock.add_response(
            url=re.compile(rf"^{re.escape(_BASE)}/items/{slug}/history\?.*"),
            json={
                "slug": slug,
                "source": "skinport",
                "tier": "curated",
                "since": "2026-05-16T00:00:00+00:00",
                "until": "2026-05-23T00:00:00+00:00",
                "count": 2,
                "observations": [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "30.00",
                        "volume": 11,
                        "timestamp": "2026-05-22T00:00:00+00:00",
                    },
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "31.25",
                        "volume": 18,
                        "timestamp": "2026-05-23T00:00:00+00:00",
                    },
                ],
            },
        )
        client = _scripted_client(
            [
                _make_msg(
                    tool_calls=[
                        _make_tool_call(
                            "query_price_history",
                            {"slug": slug, "source": "skinport", "days": 7},
                        )
                    ]
                ),
                _make_msg(content="It moved from $30.00 to $31.25 USD."),
            ]
        )

        await deepseek_client.handle_user_message(
            "how has AK Redline FT moved this week?", client=client
        )

        tool_result = _tool_json(_tool_messages_from_last_chat_call(client)[0]["content"])
        assert tool_result["slug"] == slug
        assert tool_result["count"] == 2
        assert tool_result["observations"][0]["source"] == "skinport"

    async def test_watchlist_query_routes_to_summarized_watchlist_shape(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        httpx_mock.add_response(
            url=f"{_BASE}/items",
            json=[
                {
                    "slug": "ak-47-redline-field-tested",
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "display_name": "AK-47 | Redline (Field-Tested)",
                },
                {
                    "slug": "awp-asiimov-field-tested",
                    "market_hash_name": "AWP | Asiimov (Field-Tested)",
                    "display_name": "AWP | Asiimov (Field-Tested)",
                },
            ],
        )
        client = _scripted_client(
            [
                _make_msg(tool_calls=[_make_tool_call("list_watchlist")]),
                _make_msg(content="We track rifles and snipers."),
            ]
        )

        await deepseek_client.handle_user_message(
            "what items do you track?", client=client
        )

        tool_result = _tool_json(_tool_messages_from_last_chat_call(client)[0]["content"])
        assert tool_result == {
            "count": 2,
            "by_category": {"rifle": 1, "sniper": 1},
            "sample": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "display_name": "AK-47 | Redline (Field-Tested)",
                },
                {
                    "slug": "awp-asiimov-field-tested",
                    "display_name": "AWP | Asiimov (Field-Tested)",
                },
            ],
        }

    async def test_wear_ambiguity_routes_list_then_curated_price(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        now = datetime.now(UTC)
        curated_slug = "usp-s-neo-noir-field-tested"
        httpx_mock.add_response(
            url=f"{_BASE}/items",
            json=[
                {
                    "slug": curated_slug,
                    "market_hash_name": "USP-S | Neo-Noir (Field-Tested)",
                    "display_name": "USP-S | Neo-Noir (Field-Tested)",
                    "tier": "curated",
                },
                {
                    "slug": "usp-s-neo-noir-factory-new",
                    "market_hash_name": "USP-S | Neo-Noir (Factory New)",
                    "display_name": "USP-S | Neo-Noir (Factory New)",
                    "tier": "substrate",
                },
            ],
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/{curated_slug}/price",
            json={
                "slug": curated_slug,
                "display_name": "USP-S | Neo-Noir (Field-Tested)",
                "tier": "curated",
                "sources": [
                    {
                        "source": "dmarket",
                        "denomination": "usd",
                        "price": "22.10",
                        "volume": 8,
                        "last_polled_at": (now - timedelta(minutes=3)).isoformat(),
                        "last_changed_at": (now - timedelta(minutes=3)).isoformat(),
                    }
                ],
            },
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/{curated_slug}/insights",
            json={"slug": curated_slug, "tier": "curated", "insights": []},
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/{curated_slug}/drift",
            json=_drift_payload(curated_slug),
        )
        client = _scripted_client(
            [
                _make_msg(tool_calls=[_make_tool_call("list_watchlist")]),
                _make_msg(
                    tool_calls=[
                        _make_tool_call(
                            "query_current_price", {"slug": curated_slug}
                        )
                    ]
                ),
                _make_msg(content="Use the Field-Tested wear."),
            ]
        )

        await deepseek_client.handle_user_message(
            "what's the USP-S Neo-Noir price?", client=client
        )

        tool_results = [
            _tool_json(m["content"])
            for m in _tool_messages_from_last_chat_call(client)
        ]
        assert tool_results[0]["count"] == 2
        assert tool_results[1]["slug"] == curated_slug
        assert tool_results[1]["tier"] == "curated"
        assert tool_results[1]["per_source"][1]["source"] == "dmarket"

    async def test_items_not_tracked_routes_to_price_and_surfaces_not_found(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        slug = "glock-18-fade-factory-new"
        httpx_mock.add_response(
            url=f"{_BASE}/items/{slug}/price",
            status_code=404,
        )
        client = _scripted_client(
            [
                _make_msg(tool_calls=[_make_tool_call("query_current_price", {"slug": slug})]),
                _make_msg(content="I don't track that item yet."),
            ]
        )

        await deepseek_client.handle_user_message(
            "what's the Glock Fade FN price?", client=client
        )

        tool_message = _tool_messages_from_last_chat_call(client)[0]
        assert "Not found on the api" in tool_message["content"]
        assert slug in tool_message["content"]


class TestDeepSeekClientTextOnly:
    async def test_no_tool_calls_returns_text(self) -> None:
        client = _scripted_client(
            [_make_msg(content="Plain reply, no tools called.")]
        )
        reply = await deepseek_client.handle_user_message(
            "hi", client=client
        )
        assert reply.text == "Plain reply, no tools called."
        assert reply.attachment is None
        assert client.chat.call_count == 1


class TestDeepSeekClientSingleToolCall:
    async def test_round_trip_with_tool_then_text(
        self, monkeypatch, httpx_mock
    ) -> None:
        # Mock the underlying API the tool will hit.
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        httpx_mock.add_response(
            url=f"{_BASE}/items",
            json=[{"slug": "a", "market_hash_name": "A", "display_name": "A"}],
        )
        # Two DeepSeek turns: one tool call, then a final text reply.
        client = _scripted_client(
            [
                _make_msg(
                    tool_calls=[
                        {
                            "function": {
                                "name": "list_watchlist",
                                "arguments": {},
                            }
                        }
                    ]
                ),
                _make_msg(content="There is 1 item: A."),
            ]
        )
        reply = await deepseek_client.handle_user_message(
            "what do you track?", client=client
        )
        assert "1 item" in reply.text
        assert reply.attachment is None
        assert client.chat.call_count == 2

    async def test_chart_call_returns_attachment(
        self, monkeypatch, httpx_mock
    ) -> None:
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/items/x/chart\?.*"
            ),
            content=b"\x89PNG\r\n\x1a\nbody",
            headers={"content-type": "image/png"},
        )
        client = _scripted_client(
            [
                _make_msg(
                    tool_calls=[
                        {
                            "function": {
                                "name": "render_chart",
                                "arguments": {"slug": "x"},
                            }
                        }
                    ]
                ),
                _make_msg(content="Here's the 7-day Skinport chart for X."),
            ]
        )
        reply = await deepseek_client.handle_user_message(
            "chart of x", client=client
        )
        assert reply.attachment is not None
        assert reply.attachment.content.startswith(b"\x89PNG")
        assert "chart" in reply.text.lower()


class TestDeepSeekClientDefensive:
    """Open-source model failure modes that must NOT crash the bot."""

    async def test_unknown_tool_name_handled(self) -> None:
        client = _scripted_client(
            [
                _make_msg(
                    tool_calls=[
                        {
                            "function": {
                                "name": "fly_to_the_moon",
                                "arguments": {},
                            }
                        }
                    ]
                ),
                _make_msg(content="Sorry, can't help with that."),
            ]
        )
        reply = await deepseek_client.handle_user_message(
            "do something weird", client=client
        )
        # Bot didn't crash; the model got a tool_result explaining the
        # tool doesn't exist and produced a final reply.
        assert "Sorry" in reply.text

    async def test_malformed_json_arguments_handled(
        self, monkeypatch, httpx_mock
    ) -> None:
        """Older DeepSeek versions sometimes return arguments as a
        string. Even when it's NOT valid JSON, the bot must degrade
        gracefully — pass empty args and let the tool's TypeError
        surface as a user-friendly tool_result."""
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        client = _scripted_client(
            [
                _make_msg(
                    tool_calls=[
                        {
                            "function": {
                                "name": "query_current_price",
                                "arguments": "{not real json",
                            }
                        }
                    ]
                ),
                _make_msg(content="Couldn't parse the request."),
            ]
        )
        reply = await deepseek_client.handle_user_message(
            "price of something", client=client
        )
        # Crucially: no exception escaped out to discord.py.
        assert "parse" in reply.text.lower() or "couldn't" in reply.text.lower()

    async def test_typed_exception_becomes_tool_result(
        self, monkeypatch, httpx_mock
    ) -> None:
        """A tool raising ItemNotInWatchlistError must not crash —
        its str(exc) becomes the tool_result so the LLM can phrase a
        reply."""
        monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
        monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)
        httpx_mock.add_response(
            url=f"{_BASE}/items/bogus/price", status_code=404
        )
        client = _scripted_client(
            [
                _make_msg(
                    tool_calls=[
                        {
                            "function": {
                                "name": "query_current_price",
                                "arguments": {"slug": "bogus"},
                            }
                        }
                    ]
                ),
                _make_msg(
                    content=(
                        "I don't track that item yet — ask the "
                        "operator to add it."
                    )
                ),
            ]
        )
        reply = await deepseek_client.handle_user_message(
            "price of bogus", client=client
        )
        assert "don't track" in reply.text.lower()

    async def test_runaway_loop_hits_cap(self) -> None:
        """Model keeps calling tools without ever returning text — the
        cap kicks in and we surface the canned fallback."""
        # MAX_TOOL_CALLS=5 → we need 6 chat calls (5 with tool_calls,
        # then 1 more after the cap which can also have tool_calls).
        # Script enough tool-call responses to exhaust the cap.
        tool_call = [
            {
                "function": {
                    "name": "list_watchlist",
                    "arguments": {},
                }
            }
        ]
        client = AsyncMock()
        client.chat.return_value = {
            "message": _make_msg(tool_calls=tool_call)
        }
        # Need list_watchlist to actually return for each tool exec —
        # but we don't care about the api response shape, just that
        # it doesn't crash. Patch the tool function so we don't need
        # httpx mocks for every call.
        from bot import tools as bot_tools

        client_chat_calls = 0

        def stub_list():
            nonlocal client_chat_calls
            client_chat_calls += 1
            return [{"slug": "a", "market_hash_name": "A", "display_name": "A"}]

        bot_tools.TOOL_FUNCTIONS["list_watchlist"] = stub_list
        try:
            reply = await deepseek_client.handle_user_message(
                "loop forever", client=client
            )
        finally:
            # Restore the real function for other tests.
            from bot.tools import list_watchlist as real_list

            bot_tools.TOOL_FUNCTIONS["list_watchlist"] = real_list

        assert "trouble" in reply.text.lower() or "rephrasing" in reply.text.lower()


class TestDeepSeekClientUnreachable:
    async def test_deepseek_chat_raising_returns_graceful(self) -> None:
        client = AsyncMock()
        client.chat.side_effect = ConnectionError("deepseek down")
        reply = await deepseek_client.handle_user_message(
            "anything", client=client
        )
        # No exception escaped; user-presentable text returned.
        assert "couldn't reach" in reply.text.lower() or "deepseek" in reply.text.lower()


# =====================================================================
# Section 3: bot.discord_render — pure helpers
# =====================================================================


class TestParseAllowlist:
    def test_empty_string(self) -> None:
        assert discord_render.parse_allowlist("") == set()

    def test_none(self) -> None:
        assert discord_render.parse_allowlist(None) == set()

    def test_single_id(self) -> None:
        assert discord_render.parse_allowlist("12345") == {12345}

    def test_comma_separated_with_whitespace(self) -> None:
        assert discord_render.parse_allowlist(
            " 12345 , 67890 ,  111 "
        ) == {12345, 67890, 111}

    def test_drops_non_numeric_entries(self, caplog) -> None:
        with caplog.at_level("WARNING", logger="bot.discord_render"):
            result = discord_render.parse_allowlist("12345,abc,67890")
        assert result == {12345, 67890}
        assert any("non-numeric" in r.getMessage() for r in caplog.records)


class TestIsAllowed:
    def test_empty_allowlist_rejects(self) -> None:
        assert discord_render.is_allowed(12345, set()) is False

    def test_in_allowlist_allowed(self) -> None:
        assert discord_render.is_allowed(12345, {12345, 67890}) is True

    def test_not_in_allowlist_rejected(self) -> None:
        assert (
            discord_render.is_allowed(99999, {12345, 67890}) is False
        )


class TestStripBotMention:
    def test_strips_simple_mention(self) -> None:
        result = discord_render.strip_bot_mention(
            "<@12345> what's the price?", 12345
        )
        assert result == "what's the price?"

    def test_strips_nickname_mention(self) -> None:
        # The <@!id> form was the nickname mention; some older clients
        # still emit it.
        result = discord_render.strip_bot_mention(
            "<@!12345> hi", 12345
        )
        assert result == "hi"

    def test_does_not_strip_other_mentions(self) -> None:
        result = discord_render.strip_bot_mention(
            "<@12345> ping <@67890>", 12345
        )
        assert result == "ping <@67890>"

    def test_empty_after_strip(self) -> None:
        assert (
            discord_render.strip_bot_mention("<@12345>", 12345) == ""
        )


class TestAttachmentToFile:
    def test_wraps_bytes_into_discord_file(self) -> None:
        import discord

        att = Attachment(
            content=b"\x89PNG\r\n\x1a\nbody",
            media_type="image/png",
            filename="test.png",
        )
        file = discord_render.attachment_to_file(att)
        assert isinstance(file, discord.File)
        assert file.filename == "test.png"


class TestSystemPrompt:
    def test_module_loads(self) -> None:
        from bot.system_prompt import SYSTEM_PROMPT

        assert isinstance(SYSTEM_PROMPT, str)
        # Sanity-check the prompt mentions each tool name so the
        # model's tool-call routing has the right vocabulary.
        for tool_name in TOOL_FUNCTIONS:
            assert tool_name in SYSTEM_PROMPT, (
                f"system prompt doesn't mention tool {tool_name!r}"
            )

    def test_anti_hallucination_guard_present(self) -> None:
        """Contract test on the prompt content (not a behavior test —
        we can't unit-test model output). Pins the literal strings that
        make up the anti-hallucination guard so a future prompt edit
        can't accidentally weaken it without this assertion firing.

        If you intentionally rewrite the guard, update the assertions
        to match the new wording — don't remove the test."""
        from bot.system_prompt import SYSTEM_PROMPT

        # Pin half 1: the prohibition itself, verbatim.
        assert "NEVER answer questions about prices" in SYSTEM_PROMPT, (
            "anti-hallucination guard's prohibition clause is missing"
        )

        # Pin half 2: the source-of-truth distinction (model memory
        # vs tool result). Both halves must live in the same paragraph;
        # the prompt is structured so they appear together.
        assert "from memory" in SYSTEM_PROMPT, (
            "anti-hallucination guard's 'from memory' framing is missing"
        )

        # Pin the explicit exception list — without it, the LLM might
        # over-apply the prohibition and refuse to answer "what can
        # you do?" or "what does SC mean?".
        for required_category in (
            "Meta questions",
            "Definitional questions",
            "Confirmations",
        ):
            assert required_category in SYSTEM_PROMPT, (
                f"anti-hallucination exception list missing "
                f"{required_category!r}"
            )
