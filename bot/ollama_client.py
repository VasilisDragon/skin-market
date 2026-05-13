"""Ollama tool-use loop for the Discord bot.

One async entrypoint, ``handle_user_message``, runs a multi-turn
conversation against a local Ollama daemon (default
``http://host.docker.internal:11434``) with the seven skin-market
tools wired in via ``bot.tools.TOOL_DEFINITIONS``. The loop:

1. Send ``[system_prompt, user_message]`` to Ollama.
2. If the response has ``tool_calls``, execute each tool (in
   ``asyncio.to_thread`` so blocking I/O doesn't stall the discord.py
   event loop), append the tool's serialized result to the message
   history, and loop.
3. If the response is plain text (no tool_calls), return it as the
   final reply.

Capped at ``MAX_TOOL_CALLS`` (5) sequential rounds — open-source
models occasionally get stuck calling the same tool with slightly
different arguments; the cap prevents runaway loops.

## Why the Default endpoint, not Native (load-bearing)

For the Qwen3-abliterated family on Ollama, function-calling MUST go
through the standard chat-completion endpoint with ``tools=[…]`` in
the request payload (the "Default" path in Open WebUI terms). The
"Native" tool-calling variant in some Ollama wrappers does not work
reliably for these models. We use ``ollama.AsyncClient.chat(...,
tools=...)`` which is the Default path. ADR 016 §"Default vs Native"
and the project memory entry have the full context.

## Defensive handling

Open-source models produce malformed tool calls more often than
cloud-tier APIs:

- Arguments may be returned as a JSON string instead of a dict
  (older Ollama versions).
- Tool name may not match any registered function.
- Argument dict may be missing required keys, or carry extras.
- The model may produce text and a tool call together.

All four are handled here without crashing. On any of them, we feed
a graceful error message back to the model as the tool_result so it
can produce a user-facing reply instead of looping.

## Chart attachments

When the model calls ``render_chart``, the tool returns an
``Attachment`` dataclass with PNG bytes. We stash it on the response
object (alongside a text tool_result the model can comment on) so the
discord.py layer can upload the PNG as a Discord attachment. Multiple
chart calls in one conversation are rare; we keep only the most
recent attachment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from ollama import AsyncClient

from bot.system_prompt import SYSTEM_PROMPT
from bot.tools import (
    TOOL_DEFINITIONS,
    TOOL_FUNCTIONS,
    Attachment,
    SkinMarketBotError,
)

logger = logging.getLogger(__name__)


DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434"
DEFAULT_OLLAMA_MODEL = "huihui_ai/Qwen3.6-abliterated:27b"

# Sequential tool-call cap. Open-source models occasionally loop;
# this prevents runaway. ADR 016 §"Tool-use loop".
MAX_TOOL_CALLS: int = 5

# Ollama timeout — first call after model load is 20-30s on Qwen 27b
# on the Spark; subsequent calls under KEEP_ALIVE are <2s. Generous
# enough to absorb cold-start without crashing the bot.
OLLAMA_TIMEOUT_SECONDS: float = 120.0


@dataclass
class BotReply:
    """What the Discord layer needs to render a reply.

    - ``text`` is always present (may be the canned cap/error
      message in the runaway-loop case).
    - ``attachment`` is set only when ``render_chart`` was called
      somewhere in the conversation.
    """

    text: str
    attachment: Attachment | None


def _model_name() -> str:
    return os.environ.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL


def _ollama_host() -> str:
    return (
        os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL
    )


def _normalize_arguments(raw) -> dict:
    """Older Ollama versions stringify the arguments object; newer
    return a dict. Accept either. Returns an empty dict if the value
    is missing or malformed (the executor surfaces a clear tool_result
    error in that case).
    """
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
    """Tool returns become string content on the next Ollama turn.
    Dicts/lists → JSON; Attachment → a text marker plus a hint so
    the model knows the chart was attached; otherwise ``str(...)``."""
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


async def _execute_tool(name: str, args: dict) -> tuple[str, Attachment | None]:
    """Run one tool by name. Returns ``(tool_result_text, attachment_or_None)``.

    The tool body is sync I/O; we wrap with ``asyncio.to_thread`` so
    a slow API call doesn't block the discord.py event loop.
    Typed-exception failures become user-presentable tool_result
    strings rather than propagating crashes.
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        logger.warning(
            "Ollama requested unknown tool %r; arguments=%r", name, args
        )
        return (
            f"No tool called {name!r} exists. Pick one of: "
            f"{', '.join(sorted(TOOL_FUNCTIONS))}.",
            None,
        )

    try:
        result = await asyncio.to_thread(fn, **args)
    except TypeError as exc:
        # Wrong / missing arg names — the model passed something
        # the function signature can't accept.
        logger.warning(
            "Tool %s called with bad arguments %r: %s", name, args, exc
        )
        return (
            f"Tool {name} was called with arguments it doesn't "
            f"accept: {exc}. Re-check the parameters in the tool "
            f"definition.",
            None,
        )
    except SkinMarketBotError as exc:
        logger.info(
            "Tool %s raised a typed error: %s", name, exc
        )
        return (str(exc), None)
    except Exception:
        logger.exception(
            "Tool %s raised an unexpected exception", name
        )
        return (
            f"Tool {name} hit an unexpected internal error. The "
            f"operator should check the bot logs.",
            None,
        )

    if isinstance(result, Attachment):
        return (_serialize_tool_result(result), result)
    return (_serialize_tool_result(result), None)


async def handle_user_message(
    user_message: str,
    *,
    client: AsyncClient | None = None,
) -> BotReply:
    """Run the tool-use loop for one user message.

    ``client`` is injectable for tests; production passes None and
    we construct a fresh client per call (Ollama keeps the loaded
    model warm via KEEP_ALIVE regardless of client lifetime).
    """
    client = client or AsyncClient(
        host=_ollama_host(), timeout=OLLAMA_TIMEOUT_SECONDS
    )

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    attachment: Attachment | None = None

    # Up to MAX_TOOL_CALLS tool-execution rounds; the final round
    # is for the model to produce its text reply after the last tool
    # result. ``+ 1`` is the text-only response that closes the loop.
    for round_idx in range(MAX_TOOL_CALLS + 1):
        try:
            response = await client.chat(
                model=_model_name(),
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
        except Exception as exc:
            logger.exception("Ollama chat call failed")
            return BotReply(
                text=(
                    "I couldn't reach my local LLM router right "
                    "now. The operator should check that Ollama "
                    "is running on the host. "
                    f"({type(exc).__name__})"
                ),
                attachment=attachment,
            )

        # ``response.message`` is either a Message object or a dict
        # depending on the ollama-python version; normalize.
        msg = response.message if hasattr(response, "message") else response.get("message", {})
        tool_calls = (
            msg.tool_calls
            if hasattr(msg, "tool_calls")
            else (msg.get("tool_calls") if isinstance(msg, dict) else None)
        )
        content = (
            msg.content
            if hasattr(msg, "content")
            else (msg.get("content") if isinstance(msg, dict) else "")
        )

        if not tool_calls:
            # Final text response — we're done.
            return BotReply(
                text=(content or "").strip()
                or "(empty reply from the LLM)",
                attachment=attachment,
            )

        # Append the assistant message (with tool_calls) to history
        # so subsequent turns see the tool-call provenance. We pass
        # the original message back verbatim where possible.
        messages.append(
            msg if isinstance(msg, dict) else {
                "role": "assistant",
                "content": content or "",
                "tool_calls": tool_calls,
            }
        )

        for tc in tool_calls:
            # tool_call shape: {"function": {"name": ..., "arguments": ...}}
            func = (
                tc.function
                if hasattr(tc, "function")
                else tc.get("function", {})
            )
            name = (
                func.name
                if hasattr(func, "name")
                else func.get("name", "")
            )
            raw_args = (
                func.arguments
                if hasattr(func, "arguments")
                else func.get("arguments")
            )
            args = _normalize_arguments(raw_args)

            tool_result, maybe_attachment = await _execute_tool(
                name, args
            )
            if maybe_attachment is not None:
                attachment = maybe_attachment

            messages.append(
                {
                    "role": "tool",
                    "content": tool_result,
                    # tool_call_id is required by some chat-completion
                    # implementations and ignored by others; pass when
                    # available, omit otherwise.
                    **(
                        {"tool_call_id": tc.id}
                        if hasattr(tc, "id") and tc.id
                        else {}
                    ),
                }
            )

        if round_idx >= MAX_TOOL_CALLS - 1:
            # We've consumed MAX_TOOL_CALLS rounds of tool execution;
            # the next loop iteration runs a final ollama call to
            # produce text. If that ALSO returns tool_calls, we hit
            # the loop end below.
            logger.warning(
                "Bot reached tool-call cap (%d rounds); last "
                "round will be text-only or we'll surface the cap "
                "message.",
                MAX_TOOL_CALLS,
            )

    # Fell through MAX_TOOL_CALLS + 1 rounds without a text reply —
    # the model is looping. Surface a graceful fallback.
    logger.warning(
        "Ollama exhausted %d tool-call rounds without producing a "
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
