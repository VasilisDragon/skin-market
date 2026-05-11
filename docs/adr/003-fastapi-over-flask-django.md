# ADR 003 — FastAPI over Flask or Django for the read API

**Status:** Accepted
**Date:** 2026-05-11

## Context

The application needs a small HTTP API surface (~7 endpoints in v1, per
PROJECT_SPEC.md Phase 6) that the Hermes Discord skill calls over the
network. The API is read-only against pre-computed data in Postgres, with
one POST endpoint for deal evaluation. Request shapes and response shapes
are well-defined and benefit from schema validation.

## Decision

Use FastAPI for the HTTP service. Pydantic v2 models define the request and
response schemas. Endpoints live under `api/routes/`, with one module per
resource group (prices, history, charts, deals).

## Alternatives considered

- **Flask**: simpler, well-known. But no built-in request/response
  validation, no automatic OpenAPI schema, and async support only via
  extensions. Would need Flask + Marshmallow + apispec to match FastAPI's
  out-of-the-box capabilities — at which point FastAPI is the simpler
  choice.
- **Django + DRF**: heavyweight for a read-only API with no users, no
  admin, no migrations on the API layer. The ORM is irrelevant because
  we're using SQLAlchemy directly for the time-series queries.
- **Starlette directly**: FastAPI is built on Starlette and adds the
  Pydantic integration we want. No reason to drop down a layer.
- **A pure SQL endpoint (e.g. PostgREST)**: tempting for a read-only API,
  but the deal-evaluation endpoint has non-trivial logic and the chart
  endpoint returns a PNG, both of which want application code.

## Consequences

- **Pro:** automatic OpenAPI docs at `/docs` — useful for the Hermes skill
  author and for debugging.
- **Pro:** Pydantic validation gives clear 422 errors on malformed
  requests; response models guarantee the bot sees the shape it expects.
- **Pro:** async-native, so collector or analytics workloads sharing the
  same Postgres pool don't block API responses.
- **Con:** FastAPI is younger than Flask/Django; some edges (background
  tasks, dependency-injection lifecycle) are still evolving.
- **Con:** Pydantic v2 has stricter coercion than v1 — we may hit
  schema-migration friction if we ever upgrade across a major version
  boundary again. Mitigation: pin Pydantic to `>=2.9,<3.0`.
