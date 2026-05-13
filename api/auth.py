"""Static bearer-token auth for the read API.

ADR 014 §10 evolution (Phase 7b): the auth check now accepts **any**
token from a configured set rather than a single fixed string. The
shape is unchanged for the v1 single-consumer case (one token in,
one token out), but the data structure on the receiving side is a
set, so adding a second consumer (operator CLI, future SaaS client)
is an env-var edit rather than an auth refactor.

## Env vars (in priority order; both are unioned)

- ``SKIN_MARKET_API_TOKENS`` (plural) — comma-separated list of
  accepted tokens. Whitespace around commas is stripped. Empty
  entries are dropped. This is the canonical configuration knob.
- ``SKIN_MARKET_API_TOKEN`` (singular) — single token. Convenience
  alias for the v1 single-consumer case; the bot's README points at
  this so the operator's workflow stays "set one env var".

Both env vars are read on every request (cheap; just env lookup);
their union is the accepted set. If both are empty, auth fails
**closed** with 500 — silent disabling would be a footgun. This is
the same fail-closed semantic Phase 6.6 introduced; only the storage
shape changed.

## /health stays unauthenticated

``/health`` is declared directly on the app (not inside any router),
so it bypasses this dependency. Docker healthchecks have no
credentials, and an operator running ``curl /health`` against a
misconfigured deployment must see the API's actual state instead of
a blanket 401. ADR 014 §10.

## Token shape

64-hex characters from ``openssl rand -hex 32`` — 256 bits of entropy
each. Not user-tier auth; one secret per machine boundary, no roles,
no scopes. ARCHITECTURE.md defers user accounts / roles / scopes to v5+.

## Comparison

For each token in the accepted set, we run
``secrets.compare_digest(presented, candidate)``. Constant-time per
comparison; iterating the set leaks set size to a sufficiently
patient attacker but not individual tokens. At v1 (1-3 consumers)
that's a non-concern; if the set ever grows large, switch to
``hmac.compare_digest`` against a hashed-token table.
"""

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
