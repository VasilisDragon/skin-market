"""Single static bearer-token auth for the read API.

Phase 6.6: the bot-in-compose deployment assumption from ADR 014 §3
did not survive Phase 7 contact. Hermes installs to
``~/.hermes-discord/skills/skin-market/`` — i.e. as a host process,
not as a compose service. The docker-compose internal network is no
longer a sufficient gatekeeper because the api service has to be
reachable from the host for the bot to call it.

Two coordinated changes lock the door:

1. ``docker-compose.yml`` exposes the api service via
   ``ports: ["127.0.0.1:8000:8000"]`` — host-local only, never
   ``0.0.0.0`` (same posture as Postgres).
2. This module: every authenticated route requires
   ``Authorization: Bearer <token>``, where ``<token>`` matches the
   ``SKIN_MARKET_API_TOKEN`` env var set in ``.env``.

``/health`` is the explicit exception — it stays unauthenticated:

- Docker's healthcheck calls it from inside the container without
  carrying credentials.
- A misconfigured token must not hide the api's actual health state
  from an operator running ``curl http://localhost:8000/health``;
  surfacing "the api is up but auth is broken" is more useful than a
  blanket 401.

Token shape: 64-hex-character string from ``openssl rand -hex 32`` —
256 bits of entropy. Not user-tier auth; a single machine-boundary
secret shared between the api container and whoever talks to it
(bot, operator curl, future SaaS clients).

Rotation workflow: edit ``.env``, ``docker compose up -d api``,
restart the bot. No rotation tooling in v1; manual is fine while there
is exactly one client.

Failure modes:

- Env var unset → 500 with explicit detail. Fail closed —
  silently disabling auth would be a footgun.
- ``Authorization`` header missing or not ``Bearer …`` → 401.
- Token mismatch → 401, constant-time comparison via
  ``secrets.compare_digest`` (no timing oracle).
"""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

_BEARER_PREFIX = "Bearer "


def _expected_token() -> str | None:
    """Resolve the expected token from env. Returns ``None`` if unset
    so the dependency can distinguish "unset" (operator bug, → 500)
    from "wrong" (caller bug, → 401)."""
    return os.environ.get("SKIN_MARKET_API_TOKEN")


def require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency: 401 unless a matching bearer token is
    presented. Applied to every router except ``/health`` (which is
    declared on the app directly and never enters a router include).
    """
    expected = _expected_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "API auth is not configured. Set SKIN_MARKET_API_TOKEN "
                "in the api container's environment (generate via "
                "`openssl rand -hex 32` and add to .env). See ADR 014 §10."
            ),
        )

    presented = ""
    if authorization and authorization.startswith(_BEARER_PREFIX):
        presented = authorization[len(_BEARER_PREFIX) :]

    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
