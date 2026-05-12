"""FastAPI app for the read-only skin-market API.

Mounted at the repo root as ``api.main:app``. The collector and
analytics services don't share this process — they're separate Docker
services. Reads from the same Postgres via ``db.connection.get_engine``.

Authentication: none in v1. Relies on the docker-compose internal
network as the gatekeeper — no ``ports:`` mapping is added in
``docker-compose.yml`` for this service, so it isn't exposed to the
host. ADR 014 §3 makes this dependency explicit: if Phase 7 deploys
the bot outside the compose stack, a single static bearer-token check
is the documented 15-line addition.

Endpoints:

- ``GET  /health``               — liveness + DB reachability
- ``GET  /items``                — full watchlist
- ``GET  /items/{slug}``         — single item metadata
- ``GET  /items/{slug}/price``   — per-source latest prices
- ``GET  /items/{slug}/history`` — bounded time-series
- ``GET  /items/{slug}/insights``— latest of each per-item insight
- ``GET  /items/{slug}/chart``   — PNG chart, one source × N days
- ``POST /deals/evaluate``       — verdict on a price offer
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.routes import charts, deals, history, insights, items
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

app.include_router(items.router)
app.include_router(history.router)
app.include_router(insights.router)
app.include_router(charts.router)
app.include_router(deals.router)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness + DB reachability — used by the compose healthcheck."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status: str = "reachable"
    except SQLAlchemyError:
        db_status = "unreachable"
    return HealthResponse(status="ok", db=db_status)  # type: ignore[arg-type]
