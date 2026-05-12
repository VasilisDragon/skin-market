"""Minimal Ollama client for the narrative job.

Ollama exposes an OpenAI-compatible ``/v1/chat/completions`` endpoint
when started with the default config. We use only that one endpoint;
the official Ollama Python SDK would be overkill for one call per day.

Config via env vars:

- ``OLLAMA_BASE_URL`` (default ``http://host.docker.internal:11434``)
- ``OLLAMA_MODEL``    (default ``gpt-oss:20b``)
- ``OLLAMA_TIMEOUT_SECONDS`` (default ``120``)
- ``OLLAMA_TEMPERATURE``    (default ``0.4`` — low; we want faithful
  reproduction of the input data, not creative interpretation)
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://host.docker.internal:11434"
DEFAULT_MODEL = "gpt-oss:20b"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_TEMPERATURE = 0.4


class OllamaError(RuntimeError):
    pass


def chat(
    system: str,
    user: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    temperature: float | None = None,
) -> str:
    """One-shot system+user prompt; returns the assistant's text content.

    Raises ``OllamaError`` for any failure mode — non-200, unexpected
    response shape, network timeout. The caller (narrative job) catches
    and logs without crashing the analytics cycle.
    """
    base = (base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    use_model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
    timeout = timeout or float(
        os.environ.get("OLLAMA_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    temp = temperature if temperature is not None else float(
        os.environ.get("OLLAMA_TEMPERATURE", DEFAULT_TEMPERATURE)
    )

    payload = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temp,
        "stream": False,
    }

    url = f"{base}/v1/chat/completions"
    logger.info("Ollama request: model=%s, base=%s", use_model, base)
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise OllamaError(f"network error talking to Ollama at {url}: {exc}") from exc

    if response.status_code != 200:
        raise OllamaError(
            f"Ollama returned {response.status_code}: "
            f"{response.text[:500]}"
        )

    try:
        body = response.json()
        return body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise OllamaError(
            f"Ollama response missing expected shape: {response.text[:500]}"
        ) from exc
