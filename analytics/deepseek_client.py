"""Minimal DeepSeek client for the nightly narrative job."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from db.llm_usage import DEEPSEEK_V4_FLASH_MODEL, log_llm_usage

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = DEEPSEEK_V4_FLASH_MODEL
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_TEMPERATURE = 0.2


class DeepSeekError(RuntimeError):
    pass


def _api_key() -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DeepSeekError(
            "DEEPSEEK_API_KEY environment variable is not set. "
            "Set it in .env and restart the analytics container."
        )
    return api_key


def validate_config() -> None:
    """Fail fast when the required DeepSeek key is missing."""
    _api_key()


def chat(
    system: str,
    user: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    temperature: float | None = None,
    usage_logger=log_llm_usage,
) -> str:
    """One-shot system+user prompt; returns assistant text content."""
    use_model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    base = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    timeout = timeout or float(
        os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    temp = temperature if temperature is not None else float(
        os.environ.get("DEEPSEEK_TEMPERATURE", DEFAULT_TEMPERATURE)
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temp,
        "thinking": {"type": "disabled"},
        "stream": False,
    }

    url = f"{base}/chat/completions"
    logger.info("DeepSeek request: model=%s, base=%s", use_model, base)
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise DeepSeekError(f"network error talking to DeepSeek: {exc}") from exc

    if response.status_code != 200:
        raise DeepSeekError(
            f"DeepSeek returned {response.status_code}: {response.text[:500]}"
        )

    try:
        body = response.json()
        text = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise DeepSeekError(
            f"DeepSeek response missing expected shape: {response.text[:500]}"
        ) from exc

    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise DeepSeekError("DeepSeek response missing usage block")
    usage_logger(
        model=body.get("model") or use_model,
        usage=usage,
        messages=messages,
        discord_user_id=None,
    )
    return text
