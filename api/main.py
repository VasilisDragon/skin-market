"""FastAPI app for the read-only skin-market API.

Mounted at the repo root as ``api.main:app``. The collector and
analytics services don't share this process — they're separate Docker
services. Reads from the same Postgres via ``db.connection.get_engine``.

Authentication: every router requires ``Authorization: Bearer <token>``
matching one of the configured API tokens. See ``api.auth`` and ADR 014.

``/health`` is the explicit exception — it's declared directly on the
app (not inside any router), so it skips the auth dependency entirely.
Docker's healthcheck calls it without credentials.

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
- ``GET  /insights/signals/digest`` — ranked market signal digest
- ``POST /signals/subscriptions`` — recurring Discord signal digests
- ``POST /portfolio/monitors``  — recurring portfolio baseline change monitors
- ``POST /alerts/price``         — create/list/evaluate price alerts
- ``POST /portfolio/snapshots``  — persist Discord portfolio baselines
- ``GET  /entitlements/discord/{id}`` — effective Discord quota policy
- ``POST /asset-valuations/inventory`` — public-inventory market baseline
- ``POST /asset-valuations/inventory/summary`` — portfolio market baseline
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.auth import require_token
from api.routes import (
    alerts,
    asset_valuation,
    charts,
    deals,
    drift,
    entitlements,
    history,
    insights,
    items,
    portfolio,
    portfolio_monitors,
    signals,
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
app.include_router(alerts.router, dependencies=_auth)
app.include_router(portfolio.router, dependencies=_auth)
app.include_router(portfolio_monitors.router, dependencies=_auth)
app.include_router(entitlements.router, dependencies=_auth)
app.include_router(signals.router, dependencies=_auth)
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
