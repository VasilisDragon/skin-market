"""Bot skill tool wrappers — Phase 7b.

All HTTP is mocked via ``pytest-httpx``; no network calls. The bearer
token is pinned via an autouse env-setter so the auth-injection path
is exercised on every test without the test body having to thread it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bot_skill.tools import (
    TOOLS,
    ApiAuthError,
    ApiUnexpectedError,
    ApiUnreachableError,
    Attachment,
    ItemNotInWatchlistError,
    evaluate_deal,
    list_watchlist,
    narrative_today,
    query_current_price,
    query_price_history,
    render_chart,
    whats_interesting,
)

_TEST_TOKEN = "test-bot-token-deadbeefcafe"
_BASE = "http://localhost:8001"


@pytest.fixture(autouse=True)
def _bot_env(monkeypatch):
    """Pin the env that ``tools._client`` reads. Tests can override
    by re-setting or unsetting these variables inline."""
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
    monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)


class TestTOOLSRegistry:
    """The skill registry must be the 7-tool list documented in
    SKILL.md. A renamed/dropped tool would silently break the bot's
    decision logic; this test guards against that."""

    def test_seven_tools_registered(self) -> None:
        names = {fn.__name__ for fn in TOOLS}
        assert names == {
            "list_watchlist",
            "query_current_price",
            "query_price_history",
            "render_chart",
            "evaluate_deal",
            "narrative_today",
            "whats_interesting",
        }


class TestAuthAndConnectivity:
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
            url=f"{_BASE}/items", status_code=401,
            json={"detail": "Missing or invalid bearer token."},
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


class TestListWatchlist:
    def test_happy_path(self, httpx_mock) -> None:
        payload = [
            {
                "slug": "ak-47-redline-field-tested",
                "market_hash_name": "AK-47 | Redline (Field-Tested)",
                "display_name": "AK-47 | Redline (Field-Tested)",
            }
        ]
        httpx_mock.add_response(url=f"{_BASE}/items", json=payload)
        result = list_watchlist()
        assert result == payload


class TestQueryCurrentPrice:
    def _price_resp(self, sources: list[dict]) -> dict:
        return {
            "slug": "ak-47-redline-field-tested",
            "display_name": "AK-47 | Redline (Field-Tested)",
            "sources": sources,
        }

    def _insights_resp(self, insights: list[dict]) -> dict:
        return {
            "slug": "ak-47-redline-field-tested",
            "insights": insights,
        }

    def test_three_state_fresh_unavailable_never_observed(
        self, httpx_mock
    ) -> None:
        """The composer must categorize each EXPECTED_SOURCES entry:
        - skinport in /price → fresh
        - steam_market not in /price + streak insight → unavailable
        - dmarket absent everywhere → never_observed
        """
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=(
                f"{_BASE}/items/ak-47-redline-field-tested/price"
            ),
            json=self._price_resp(
                [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "33.06",
                        "volume": 521,
                        "observed_at": (
                            (now - timedelta(minutes=10)).isoformat()
                        ),
                    }
                ]
            ),
        )
        httpx_mock.add_response(
            url=(
                f"{_BASE}/items/ak-47-redline-field-tested/insights"
            ),
            json=self._insights_resp(
                [
                    {
                        "insight_type": "item_unavailability_streak",
                        "computed_at": now.isoformat(),
                        "value": "3",
                        "text_value": None,
                        "meta": {
                            "source_name": "steam_market",
                            "streak_cycles": 3,
                            "last_seen_observed": (
                                (now - timedelta(hours=4)).isoformat()
                            ),
                            "first_seen_unavailable": (
                                (now - timedelta(hours=3)).isoformat()
                            ),
                        },
                    }
                ]
            ),
        )

        result = query_current_price("ak-47-redline-field-tested")
        by_source = {p["source"]: p for p in result["per_source"]}
        assert by_source["skinport"]["state"] == "fresh"
        assert by_source["skinport"]["price"] == "33.06"
        assert by_source["steam_market"]["state"] == "unavailable"
        assert by_source["steam_market"]["streak_cycles"] == 3
        assert by_source["dmarket"]["state"] == "never_observed"
        # Denomination tagged on every source, including never_observed.
        assert (
            by_source["steam_market"]["denomination"] == "wallet_credit"
        )
        assert by_source["dmarket"]["denomination"] == "usd"
        assert result["anomaly_flag"] is None

    def test_stale_state_when_observation_older_than_4h(
        self, httpx_mock
    ) -> None:
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json=self._price_resp(
                [
                    {
                        "source": "skinport",
                        "denomination": "usd",
                        "price": "28.00",
                        "volume": 27,
                        "observed_at": (
                            (now - timedelta(hours=5)).isoformat()
                        ),
                    }
                ]
            ),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights_resp([]),
        )

        result = query_current_price("x")
        sk = next(
            p for p in result["per_source"] if p["source"] == "skinport"
        )
        assert sk["state"] == "stale"
        assert sk["minutes_since_observed"] >= 4 * 60

    def test_anomaly_flag_set_when_divergence_recent(
        self, httpx_mock
    ) -> None:
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json=self._price_resp([]),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights_resp(
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
                            "observed_spread": 0.37,
                            "baseline_mean": 0.45,
                            "baseline_stddev": 0.028,
                        },
                    }
                ]
            ),
        )

        result = query_current_price("x")
        assert result["anomaly_flag"] is not None
        assert result["anomaly_flag"]["z_score"] == "-2.89"
        assert "below" in result["anomaly_flag"]["summary"]

    def test_anomaly_flag_omitted_when_divergence_stale(
        self, httpx_mock
    ) -> None:
        """A divergence row older than ANOMALY_FRESHNESS_HOURS should
        NOT set the anomaly flag — z-scores reset every hour, so a
        24h-old divergence is no longer "currently firing"."""
        now = datetime.now(UTC)
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/price",
            json=self._price_resp([]),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights_resp(
                [
                    {
                        "insight_type": "cross_source_divergence",
                        "computed_at": (
                            (now - timedelta(hours=24)).isoformat()
                        ),
                        "value": "-2.89",
                        "text_value": None,
                        "meta": {
                            "source_a_id": "1", "source_b_id": "27",
                        },
                    }
                ]
            ),
        )

        result = query_current_price("x")
        assert result["anomaly_flag"] is None

    def test_404_raises_item_not_in_watchlist(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/items/bogus/price",
            status_code=404,
            json={"detail": "Item not found"},
        )
        with pytest.raises(ItemNotInWatchlistError):
            query_current_price("bogus")


class TestQueryPriceHistory:
    def test_happy_path_with_source_filter(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/items/x/history\?.*source=skinport.*$"
            ),
            json={
                "slug": "x",
                "source": "skinport",
                "since": "...",
                "until": "...",
                "limit": 500,
                "count": 0,
                "observations": [],
            },
        )
        result = query_price_history("x", source="skinport", days=3)
        assert result["source"] == "skinport"


class TestRenderChart:
    def test_returns_attachment_with_png_bytes(
        self, httpx_mock
    ) -> None:
        png_magic = b"\x89PNG\r\n\x1a\n" + b"fake-body"
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/items/x/chart\?.*"
            ),
            content=png_magic,
            headers={"content-type": "image/png"},
        )
        att = render_chart("x", source="skinport", days=7)
        assert isinstance(att, Attachment)
        assert att.media_type == "image/png"
        assert att.content.startswith(b"\x89PNG")
        assert att.filename == "x-skinport-7d.png"

    def test_404_raises_item_not_in_watchlist(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}/items/bogus/chart.*"
            ),
            status_code=404,
        )
        with pytest.raises(ItemNotInWatchlistError):
            render_chart("bogus")


class TestEvaluateDeal:
    def test_happy_path_posts_offer(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/deals/evaluate",
            json={
                "slug": "ak-47-redline-field-tested",
                "display_name": "AK-47 | Redline (Field-Tested)",
                "offer": {"amount": "42.50", "currency": "usd"},
                "verdict": "above_market",
                "comparable": [],
                "informational": [],
                "summary": "...",
            },
        )
        result = evaluate_deal(
            "ak-47-redline-field-tested",
            amount="42.50",
            currency="usd",
        )
        assert result["verdict"] == "above_market"
        # Request body carries the offer shape the API expects.
        req = httpx_mock.get_requests()[0]
        import json as _json

        body = _json.loads(req.content.decode())
        assert body == {
            "slug": "ak-47-redline-field-tested",
            "offer": {"amount": "42.50", "currency": "usd"},
        }

    def test_404_raises_item_not_in_watchlist(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/deals/evaluate", status_code=404
        )
        with pytest.raises(ItemNotInWatchlistError):
            evaluate_deal("bogus", amount="1.00", currency="usd")


class TestNarrativeToday:
    def test_happy_path(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/insights/narrative/latest",
            json={
                "computed_at": "2026-05-13T03:00:00Z",
                "text": "Today, the AWP | Hyper Beast ...",
                "meta": {"as_of": "..."},
            },
        )
        result = narrative_today()
        assert result["text"].startswith("Today")

    def test_404_when_no_narrative_yet(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/insights/narrative/latest", status_code=404
        )
        with pytest.raises(ItemNotInWatchlistError):
            narrative_today()


class TestWhatsInteresting:
    def test_happy_path_default_hours(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}"
                r"/insights/anomalies/recent\?hours=6$"
            ),
            json={
                "since": "...",
                "count": 1,
                "anomalies": [
                    {
                        "insight_type": "cross_source_divergence",
                        "slug": "x",
                        "display_name": "X",
                        "computed_at": "...",
                        "z_score": "-2.89",
                        "meta": {},
                    }
                ],
            },
        )
        result = whats_interesting()
        assert result["count"] == 1

    def test_custom_hours_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=re.compile(
                rf"^{re.escape(_BASE)}"
                r"/insights/anomalies/recent\?hours=12$"
            ),
            json={"since": "...", "count": 0, "anomalies": []},
        )
        whats_interesting(hours=12)


class TestDocstringDiscovery:
    """Hermes' loader may introspect tool docstrings for descriptions.
    Each tool must have a non-trivial docstring that includes its
    intended use; this test guards against ``""``/None drift over
    time."""

    def test_every_tool_has_docstring(self) -> None:
        for fn in TOOLS:
            doc = (fn.__doc__ or "").strip()
            assert len(doc) >= 60, (
                f"Tool {fn.__name__} docstring too short: "
                f"{len(doc)} chars (need at least 60 for the LLM to "
                f"reason about when to call it)."
            )
