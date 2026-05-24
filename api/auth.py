"""Static bearer-token auth for the read API."""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

_BEARER_PREFIX = "Bearer "


def _accepted_tokens() -> set[str]:
    """Union of tokens from ``SKIN_MARKET_API_TOKENS`` (plural,
    comma-separated) and ``SKIN_MARKET_API_TOKEN`` (singular alias).
    Whitespace stripped; empty entries dropped."""
    accepted: set[str] = set()
    plural = os.environ.get("SKIN_MARKET_API_TOKENS", "")
    for raw in plural.split(","):
        token = raw.strip()
        if token:
            accepted.add(token)
    singular = (os.environ.get("SKIN_MARKET_API_TOKEN") or "").strip()
    if singular:
        accepted.add(singular)
    return accepted


def require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency: 401 unless a matching bearer token is
    presented. Applied to every router except ``/health``."""
    accepted = _accepted_tokens()
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "API auth is not configured. Set "
                "SKIN_MARKET_API_TOKENS (comma-separated) or "
                "SKIN_MARKET_API_TOKEN (single) in the api "
                "container's environment. Generate tokens via "
                "`openssl rand -hex 32`. See ADR 014 §10."
            ),
        )

    presented = ""
    if authorization and authorization.startswith(_BEARER_PREFIX):
        presented = authorization[len(_BEARER_PREFIX) :]

    # secrets.compare_digest per candidate keeps each comparison
    # constant-time; iterating the set leaks set size but not token
    # contents. Fine at v1's 1-3 consumer scale.
    for candidate in accepted:
        if secrets.compare_digest(presented, candidate):
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid bearer token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
