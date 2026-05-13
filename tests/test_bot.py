"""Bot tests — Phase 7c.

Three test modules in one file:

1. ``TestTools*`` — ported from the Phase 7b ``test_bot_skill.py``
   suite. The wrapper functions in ``bot.tools`` are the same code
   (re-pointed at ``http://api:8000`` for the in-compose path).
   pytest-httpx mocks all HTTP; no network.
2. ``TestOllamaClient*`` — the tool-use loop in
   ``bot.ollama_client``. Ollama's ``AsyncClient`` is mocked via a
   small stub class so we don't need a running Ollama. Each test
   scripts the responses the model would produce, including the
   defensive cases (malformed args, unknown tool, runaway loop).
3. ``TestDiscordRender`` — pure-Python helpers (allowlist parsing,
   mention stripping). discord.py is only imported by
   ``attachment_to_file`` and that path is tested separately.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from bot import discord_render, ollama_client
from bot.tools import (
    TOOL_DEFINITIONS,
    TOOL_FUNCTIONS,
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
_BASE = "http://api-test-host:8000"


@pytest.fixture(autouse=True)
def _bot_env(monkeypatch):
    """Pin the env that ``bot.tools._client`` reads."""
    monkeypatch.setenv("SKIN_MARKET_API_TOKEN", _TEST_TOKEN)
    monkeypatch.setenv("SKIN_MARKET_API_BASE_URL", _BASE)


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


class TestQueryCurrentPriceComposer:
    """The three-state composer is the only non-trivial tool. Re-test
    the categorization, the anomaly flag, and the never_observed
    fallback so a refactor can't quietly break the bot's rendering."""

    @staticmethod
    def _price(sources: list[dict]) -> dict:
        return {
            "slug": "x",
            "display_name": "X",
            "sources": sources,
        }

    @staticmethod
    def _insights(rows: list[dict]) -> dict:
        return {"slug": "x", "insights": rows}

    def test_fresh_unavailable_never_observed(
        self, httpx_mock
    ) -> None:
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
                        "observed_at": (
                            (now - timedelta(minutes=10)).isoformat()
                        ),
                    }
                ]
            ),
        )
        httpx_mock.add_response(
            url=f"{_BASE}/items/x/insights",
            json=self._insights(
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
        result = query_current_price("x")
        states = {p["source"]: p["state"] for p in result["per_source"]}
        assert states == {
            "skinport": "fresh",
            "steam_market": "unavailable",
            "dmarket": "never_observed",
        }
        # Denomination tagged on never_observed too.
        denoms = {
            p["source"]: p["denomination"] for p in result["per_source"]
        }
        assert denoms["dmarket"] == "usd"
        assert denoms["steam_market"] == "wallet_credit"
        assert result["anomaly_flag"] is None

    def test_stale_when_older_than_4h(self, httpx_mock) -> None:
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
                        "observed_at": (
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
        result = query_current_price("x")
        sk = next(p for p in result["per_source"] if p["source"] == "skinport")
        assert sk["state"] == "stale"

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
# Section 2: bot.ollama_client — tool-use loop
# =====================================================================


def _make_msg(content="", tool_calls=None):
    """Mimic the dict shape an older Ollama version returns (the
    ollama-python client also accepts dicts here)."""
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _scripted_client(responses: list[dict]):
    """Build a mock AsyncClient whose ``chat`` returns each response
    in order."""
    client = AsyncMock()
    client.chat.side_effect = [{"message": r} for r in responses]
    return client


class TestOllamaClientTextOnly:
    async def test_no_tool_calls_returns_text(self) -> None:
        client = _scripted_client(
            [_make_msg(content="Plain reply, no tools called.")]
        )
        reply = await ollama_client.handle_user_message(
            "hi", client=client
        )
        assert reply.text == "Plain reply, no tools called."
        assert reply.attachment is None
        assert client.chat.call_count == 1


class TestOllamaClientSingleToolCall:
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
        # Two Ollama turns: one tool call, then a final text reply.
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
        reply = await ollama_client.handle_user_message(
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
        reply = await ollama_client.handle_user_message(
            "chart of x", client=client
        )
        assert reply.attachment is not None
        assert reply.attachment.content.startswith(b"\x89PNG")
        assert "chart" in reply.text.lower()


class TestOllamaClientDefensive:
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
        reply = await ollama_client.handle_user_message(
            "do something weird", client=client
        )
        # Bot didn't crash; the model got a tool_result explaining the
        # tool doesn't exist and produced a final reply.
        assert "Sorry" in reply.text

    async def test_malformed_json_arguments_handled(
        self, monkeypatch, httpx_mock
    ) -> None:
        """Older Ollama versions sometimes return arguments as a
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
        reply = await ollama_client.handle_user_message(
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
        reply = await ollama_client.handle_user_message(
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
            reply = await ollama_client.handle_user_message(
                "loop forever", client=client
            )
        finally:
            # Restore the real function for other tests.
            from bot.tools import list_watchlist as real_list

            bot_tools.TOOL_FUNCTIONS["list_watchlist"] = real_list

        assert "trouble" in reply.text.lower() or "rephrasing" in reply.text.lower()


class TestOllamaClientUnreachable:
    async def test_ollama_chat_raising_returns_graceful(self) -> None:
        client = AsyncMock()
        client.chat.side_effect = ConnectionError("ollama down")
        reply = await ollama_client.handle_user_message(
            "anything", client=client
        )
        # No exception escaped; user-presentable text returned.
        assert "couldn't reach" in reply.text.lower() or "ollama" in reply.text.lower()


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
