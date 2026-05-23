"""FastAPI app for the read-only skin-market API.

Mounted at the repo root as ``api.main:app``. The collector and
analytics services don't share this process — they're separate Docker
services. Reads from the same Postgres via ``db.connection.get_engine``.

Authentication (Phase 6.6, ADR 014 §10): every router requires
``Authorization: Bearer <token>`` matching the
``SKIN_MARKET_API_TOKEN`` env var. The previous "no auth, compose
network is the gatekeeper" posture (ADR 014 §3) did not survive Phase
7 contact — Hermes installs to ``~/.hermes-discord/`` as a host
process, so the api service must be reachable from the host. The
single-token bearer dependency in ``api.auth.require_token`` is the
documented escape hatch ADR 014 §3 anticipated.

``/health`` is the explicit exception — it's declared directly on the
app (not inside any router), so it skips the auth dependency entirely.
Docker's healthcheck calls it without credentials, and surfacing the
api's actual state when auth is misconfigured is more useful than a
blanket 401. ADR 014 §10 has the rationale.

Endpoints:

- ``GET  /health``               — unauthenticated; liveness + DB reachability
- ``GET  /items``                — full watchlist
- ``GET  /items/{slug}``         — single item metadata
- ``GET  /items/{slug}/price``   — per-source latest prices
- ``GET  /items/{slug}/history`` — bounded time-series
- ``GET  /items/{slug}/insights``— latest of each per-item insight
- ``GET  /items/{slug}/chart``   — PNG chart, one source × N days
- ``GET  /items/{slug}/drift``   — latest drift verdict per pair
- ``POST /deals/evaluate``       — verdict on a price offer
- ``POST /asset-valuations/inventory`` — public-inventory market baseline
- ``POST /asset-valuations/inventory/summary`` — portfolio market baseline
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.auth import require_token
from api.routes import (
    asset_valuation,
    charts,
    deals,
    drift,
    history,
    insights,
    items,
)
from api.schemas import HealthResponse
from db.connection import get_engine

app = FastAPI(
    title="skin-market read API",
    description=(
        "Read-only access to CS2 skin price data, per-source insights, "
        "and deal evaluation. Every price field carries its source's "
        "denomination tag — `usd` vs `wallet_credit` are surfaced "
        "separately and never collapsed. See ADR 014 and "
        "docs/sources-and-semantics.md."
    ),
    version="0.1.0",
)

# Auth applies to every router included below. /health is declared
# directly on the app and never enters a router include, so it stays
# open. ADR 014 §10.
_auth = [Depends(require_token)]

app.include_router(items.router, dependencies=_auth)
app.include_router(history.router, dependencies=_auth)
app.include_router(insights.router, dependencies=_auth)
app.include_router(charts.router, dependencies=_auth)
app.include_router(deals.router, dependencies=_auth)
app.include_router(drift.router, dependencies=_auth)
app.include_router(asset_valuation.router, dependencies=_auth)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness + DB reachability — used by the compose healthcheck.

    Unauthenticated by design (ADR 014 §10): the Docker healthcheck
    calls this from inside the container without credentials, and a
    misconfigured auth token must not hide the api's actual state from
    a host-side ``curl http://localhost:8000/health``.
    """
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status: str = "reachable"
    except SQLAlchemyError:
        db_status = "unreachable"
    return HealthResponse(status="ok", db=db_status)  # type: ignore[arg-type]
