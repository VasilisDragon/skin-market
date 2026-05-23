"""Helpers for logging DeepSeek token usage and per-request cost."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.models import LLMUsageLog

DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"

# Official DeepSeek prices for deepseek-v4-flash, verified 2026-05-23:
# units are USD per 1M tokens. See docs/adr/026-deepseek-inference.md.
DEEPSEEK_V4_FLASH_INPUT_CACHE_HIT_PER_MILLION = Decimal("0.0028")
DEEPSEEK_V4_FLASH_INPUT_CACHE_MISS_PER_MILLION = Decimal("0.14")
DEEPSEEK_V4_FLASH_OUTPUT_PER_MILLION = Decimal("0.28")

_ONE_MILLION = Decimal("1000000")
_COST_QUANT = Decimal("0.00000001")
_PREVIEW_CHARS = 200
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|token)([\"'=:\s]+)([^\"'\s,}]+)"
)


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _serialize_prompt(messages: list[dict[str, Any]]) -> str:
    return json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _redact_prompt_preview(prompt: str) -> str:
    redacted = _SECRET_RE.sub(r"\1\2[REDACTED]", prompt)
    return redacted.replace("\n", " ")[:_PREVIEW_CHARS]


def _usage_int(usage: dict[str, Any], key: str, default: int | None = None) -> int:
    raw = usage.get(key)
    if raw is None:
        if default is None:
            raise ValueError(f"DeepSeek usage block missing {key!r}")
        return default
    return int(raw)


def compute_deepseek_v4_flash_cost(usage: dict[str, Any]) -> Decimal:
    """Return USD cost for one DeepSeek v4-flash request."""
    prompt_tokens = _usage_int(usage, "prompt_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens")
    hit_tokens = _usage_int(usage, "prompt_cache_hit_tokens", 0)
    miss_tokens = _usage_int(
        usage,
        "prompt_cache_miss_tokens",
        max(prompt_tokens - hit_tokens, 0),
    )
    cost = (
        Decimal(hit_tokens) * DEEPSEEK_V4_FLASH_INPUT_CACHE_HIT_PER_MILLION
        + Decimal(miss_tokens) * DEEPSEEK_V4_FLASH_INPUT_CACHE_MISS_PER_MILLION
        + Decimal(completion_tokens) * DEEPSEEK_V4_FLASH_OUTPUT_PER_MILLION
    ) / _ONE_MILLION
    return cost.quantize(_COST_QUANT)


def build_llm_usage_row(
    *,
    model: str,
    usage: dict[str, Any],
    messages: list[dict[str, Any]],
    discord_user_id: str | None = None,
    request_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build an insertable ``llm_usage_log`` row from a DeepSeek response."""
    prompt = _serialize_prompt(messages)
    prompt_tokens = _usage_int(usage, "prompt_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens")
    total_tokens = _usage_int(
        usage, "total_tokens", prompt_tokens + completion_tokens
    )
    hit_tokens = _usage_int(usage, "prompt_cache_hit_tokens", 0)
    miss_tokens = _usage_int(
        usage, "prompt_cache_miss_tokens", max(prompt_tokens - hit_tokens, 0)
    )
    return {
        "request_id": request_id or uuid.uuid4(),
        "discord_user_id": discord_user_id,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_cache_hit_tokens": hit_tokens,
        "prompt_cache_miss_tokens": miss_tokens,
        "input_cache_hit_price_per_million": (
            DEEPSEEK_V4_FLASH_INPUT_CACHE_HIT_PER_MILLION
        ),
        "input_cache_miss_price_per_million": (
            DEEPSEEK_V4_FLASH_INPUT_CACHE_MISS_PER_MILLION
        ),
        "output_price_per_million": DEEPSEEK_V4_FLASH_OUTPUT_PER_MILLION,
        "cost_usd": compute_deepseek_v4_flash_cost(usage),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_preview": _redact_prompt_preview(prompt),
        "full_prompt": prompt if _truthy_env("LLM_USAGE_LOG_FULL_PROMPT") else None,
        "raw_usage": usage,
    }


def log_llm_usage(
    *,
    model: str,
    usage: dict[str, Any],
    messages: list[dict[str, Any]],
    discord_user_id: str | None = None,
    engine: Engine | None = None,
) -> uuid.UUID:
    """Insert one ``llm_usage_log`` row and return its request_id."""
    row = build_llm_usage_row(
        model=model,
        usage=usage,
        messages=messages,
        discord_user_id=discord_user_id,
    )
    use_engine = engine or get_engine()
    with Session(use_engine) as session:
        session.execute(insert(LLMUsageLog), [row])
        session.commit()
    return row["request_id"]
