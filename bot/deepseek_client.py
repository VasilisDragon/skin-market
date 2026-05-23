"""DeepSeek tool-use loop for the Discord bot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from bot.system_prompt import SYSTEM_PROMPT
from bot.tools import (
    TOOL_DEFINITIONS,
    TOOL_FUNCTIONS,
    Attachment,
    SkinMarketBotError,
)
from db.llm_usage import (
    DEEPSEEK_V4_FLASH_MODEL,
    log_llm_usage,
    sum_llm_usage_cost_since,
)

logger = logging.getLogger(__name__)


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = DEEPSEEK_V4_FLASH_MODEL
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 120.0
MAX_TOOL_CALLS: int = 5
CONTEXT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_price_alert",
        "list_price_alerts",
        "cancel_price_alert",
        "save_portfolio_snapshot",
        "list_portfolio_snapshots",
        "portfolio_snapshot_trend",
        "create_portfolio_monitor",
        "list_portfolio_monitors",
        "cancel_portfolio_monitor",
        "create_signal_subscription",
        "list_signal_subscriptions",
        "cancel_signal_subscription",
    }
)


class DeepSeekError(RuntimeError):
    pass


class DeepSeekBudgetExceeded(RuntimeError):
    pass


@dataclass
class BotReply:
    """What the Discord layer needs to render a reply."""

    text: str
    attachment: Attachment | None


class DeepSeekChatClient:
    """Small async client for DeepSeek's OpenAI-format chat endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        discord_user_id: str | None = None,
        usage_logger=log_llm_usage,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or _deepseek_base_url()).rstrip("/")
        self._timeout = timeout or _deepseek_timeout()
        self._discord_user_id = discord_user_id
        self._usage_logger = usage_logger

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict:
        api_key = self._api_key or _deepseek_api_key()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "thinking": {"type": "disabled"},
        }
        if tools is not None:
            payload["tools"] = tools

        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise DeepSeekError(f"network error talking to DeepSeek: {exc}") from exc

        if response.status_code != 200:
            raise DeepSeekError(
                f"DeepSeek returned {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise DeepSeekError(
                f"DeepSeek response missing expected shape: {response.text[:500]}"
            ) from exc

        usage = body.get("usage")
        if not isinstance(usage, dict):
            raise DeepSeekError("DeepSeek response missing usage block")
        logged_model = body.get("model") or model
        self._usage_logger(
            model=logged_model,
            usage=usage,
            messages=messages,
            discord_user_id=self._discord_user_id,
        )
        return {"message": message, "usage": usage, "id": body.get("id")}


def _deepseek_api_key() -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DeepSeekError(
            "DEEPSEEK_API_KEY environment variable is not set. "
            "Set it in .env and restart the bot container."
        )
    return api_key


def validate_config() -> None:
    """Fail fast when the required DeepSeek key is missing."""
    _deepseek_api_key()


def _deepseek_base_url() -> str:
    return os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL


def _deepseek_model() -> str:
    return os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL


def _deepseek_timeout() -> float:
    return float(
        os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", DEFAULT_DEEPSEEK_TIMEOUT_SECONDS)
    )


def _decimal_env_limit(name: str) -> Decimal | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise DeepSeekError(f"{name} must be a decimal USD amount") from exc
    if value <= 0:
        return None
    return value


def _check_deepseek_budget(discord_user_id: str | None) -> None:
    """Fail fast when configured daily DeepSeek spend limits are reached."""
    global_limit = _decimal_env_limit("DEEPSEEK_DAILY_COST_LIMIT_USD")
    user_limit = _decimal_env_limit("DEEPSEEK_DAILY_USER_COST_LIMIT_USD")
    if global_limit is None and user_limit is None:
        return

    since = datetime.now(UTC) - timedelta(hours=24)
    if global_limit is not None:
        global_spend = sum_llm_usage_cost_since(since=since)
        if global_spend >= global_limit:
            raise DeepSeekBudgetExceeded(
                "The bot's 24-hour DeepSeek budget is exhausted. "
                "The operator can raise DEEPSEEK_DAILY_COST_LIMIT_USD "
                "or wait for spend to fall out of the rolling window."
            )

    if user_limit is not None and discord_user_id is not None:
        user_spend = sum_llm_usage_cost_since(
            since=since,
            discord_user_id=discord_user_id,
        )
        if user_spend >= user_limit:
            raise DeepSeekBudgetExceeded(
                "Your 24-hour DeepSeek budget is exhausted. Try again later "
                "or ask the operator to raise DEEPSEEK_DAILY_USER_COST_LIMIT_USD."
            )


def _normalize_arguments(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _serialize_tool_result(result) -> str:
    if isinstance(result, Attachment):
        return (
            f"Chart generated successfully and attached to your "
            f"reply (filename={result.filename}, "
            f"{len(result.content)} bytes). Add a one-line text "
            f"comment describing what the chart covers."
        )
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)
    return str(result)


async def _execute_tool(
    name: str,
    args: dict,
    *,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> tuple[str, Attachment | None]:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        logger.warning("DeepSeek requested unknown tool %r; arguments=%r", name, args)
        return (
            f"No tool called {name!r} exists. Pick one of: "
            f"{', '.join(sorted(TOOL_FUNCTIONS))}.",
            None,
        )

    if name in CONTEXT_TOOL_NAMES:
        args = {
            **args,
            "discord_user_id": discord_user_id,
            "discord_channel_id": discord_channel_id,
        }

    try:
        result = await asyncio.to_thread(fn, **args)
    except TypeError as exc:
        logger.warning("Tool %s called with bad arguments %r: %s", name, args, exc)
        return (
            f"Tool {name} was called with arguments it doesn't "
            f"accept: {exc}. Re-check the parameters in the tool "
            f"definition.",
            None,
        )
    except SkinMarketBotError as exc:
        logger.info("Tool %s raised a typed error: %s", name, exc)
        return (str(exc), None)
    except Exception:
        logger.exception("Tool %s raised an unexpected exception", name)
        return (
            f"Tool {name} hit an unexpected internal error. The "
            f"operator should check the bot logs.",
            None,
        )

    if isinstance(result, Attachment):
        return (_serialize_tool_result(result), result)
    return (_serialize_tool_result(result), None)


def _tool_call_function(tool_call) -> Any:
    if hasattr(tool_call, "function"):
        return tool_call.function
    if isinstance(tool_call, dict):
        return tool_call.get("function", {})
    return {}


def _tool_call_id(tool_call) -> str | None:
    if hasattr(tool_call, "id") and tool_call.id:
        return tool_call.id
    if isinstance(tool_call, dict):
        return tool_call.get("id")
    return None


def _field(obj, name: str, default=None):
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


async def handle_user_message(
    user_message: str,
    *,
    client: DeepSeekChatClient | None = None,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> BotReply:
    """Run the tool-use loop for one user message."""
    client = client or DeepSeekChatClient(discord_user_id=discord_user_id)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    attachment: Attachment | None = None

    for round_idx in range(MAX_TOOL_CALLS + 1):
        try:
            _check_deepseek_budget(discord_user_id)
            response = await client.chat(
                model=_deepseek_model(),
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
        except DeepSeekBudgetExceeded as exc:
            logger.warning("%s", exc)
            return BotReply(text=str(exc), attachment=attachment)
        except Exception as exc:
            logger.exception("DeepSeek chat call failed")
            return BotReply(
                text=(
                    "I couldn't reach my DeepSeek LLM router right "
                    "now. The operator should check DEEPSEEK_API_KEY, "
                    "network reachability, and usage logging. "
                    f"({type(exc).__name__})"
                ),
                attachment=attachment,
            )

        msg = response["message"] if isinstance(response, dict) else {}
        tool_calls = _field(msg, "tool_calls")
        content = _field(msg, "content", "")

        if not tool_calls:
            return BotReply(
                text=(content or "").strip() or "(empty reply from the LLM)",
                attachment=attachment,
            )

        messages.append(
            msg
            if isinstance(msg, dict)
            else {
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            }
        )

        for tc in tool_calls:
            func = _tool_call_function(tc)
            name = _field(func, "name", "")
            raw_args = _field(func, "arguments")
            args = _normalize_arguments(raw_args)

            tool_result, maybe_attachment = await _execute_tool(
                name,
                args,
                discord_user_id=discord_user_id,
                discord_channel_id=discord_channel_id,
            )
            if maybe_attachment is not None:
                attachment = maybe_attachment

            tool_message = {"role": "tool", "content": tool_result}
            call_id = _tool_call_id(tc)
            if call_id:
                tool_message["tool_call_id"] = call_id
            messages.append(tool_message)

        if round_idx >= MAX_TOOL_CALLS - 1:
            logger.warning(
                "Bot reached tool-call cap (%d rounds); last "
                "round will be text-only or we'll surface the cap "
                "message.",
                MAX_TOOL_CALLS,
            )

    logger.warning(
        "DeepSeek exhausted %d tool-call rounds without producing a "
        "final text response. Returning the cap message.",
        MAX_TOOL_CALLS,
    )
    return BotReply(
        text=(
            "I had trouble answering that — could you try "
            "rephrasing? (My tool-use loop hit its limit.)"
        ),
        attachment=attachment,
    )
