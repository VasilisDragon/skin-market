# ARCHITECTURE.md - skin-market

## Project Summary

`skin-market` is a locally hosted CS2 skin market intelligence service with
deterministic collectors, a FastAPI read API, analytics jobs, and a Discord bot
that uses an LLM only for question parsing, tool routing, and response wording.

The LLM must not fetch market data, scrape upstreams, parse external payloads, or
compute valuations. Those responsibilities stay in Python code backed by
Postgres.

## Engineering Rules

- State the expected behavior before debugging a symptom.
- Keep secrets in environment variables only. Never commit `.env`, backups, API
  keys, Discord tokens, database passwords, or private operator data.
- Add ADRs for non-obvious architecture choices: schema shape, external service
  integration, API contracts, model/provider selection, and pricing methodology.
- Use familiar, debuggable libraries: `httpx`, `psycopg`, `sqlalchemy`,
  `fastapi`, `apscheduler`, `matplotlib`, `pandas`, and `pytest`.
- Prefer clear Python over clever Python. Public functions should have type
  hints; docstrings should explain contracts, not restate implementation.
- Use `Decimal` for money and PostgreSQL `NUMERIC` for persisted prices.
- Do not merge schema-dependent code ahead of its migration. For additive
  migrations, apply the migration, verify the schema, then run code tests. For
  destructive migrations, test compatibility before applying the change.
- Before pushing, run both `uv run ruff check .` and `uv run pytest`.
- Rebuild Docker services when code changes are baked into images:
  `docker compose up -d --build SERVICE`. Use `docker compose restart SERVICE`
  only for runtime configuration changes.

## Architecture

Data flow:

```text
Steam / Skinport / DMarket / Pricempire
        -> collectors
        -> Postgres + TimescaleDB
        -> analytics jobs
        -> FastAPI read API
        -> Discord bot
        -> DeepSeek for tool routing and prose only
```

Layer responsibilities:

- **Collectors:** Fetch upstream market data, normalize payloads, and persist
  observations. Per-item collectors write curated source prices. The Pricempire
  collector handles bulk catalog snapshots and writes provider-specific rows.
- **Database:** Source of truth for items, sources, price observations, metadata,
  analytics output, entitlements, alert state, and LLM usage accounting.
- **Analytics:** Computes deterministic market signals such as moving averages,
  divergence, volume anomalies, digest candidates, and portfolio baseline
  changes.
- **FastAPI:** Read API for bot-facing and future product surfaces. The bot
  should access market data through this layer rather than direct database reads.
- **Discord bot:** Converts user questions into API tool calls and renders
  responses. The only direct database write in the bot path is LLM usage
  accounting.

## Repository Layout

```text
analytics/   Deterministic market analytics jobs
api/         FastAPI app, schemas, and route handlers
bot/         Discord bot, LLM tool loop, renderers, and API wrappers
collectors/  Steam, Skinport, DMarket, Pricempire, and scheduler code
data/        Watchlist and seed inputs
db/          SQLAlchemy models and Alembic migrations
docs/adr/    Architecture decision records
scripts/     Operator utilities and seed workflows
tests/       Unit and integration tests
```

## Boundaries

- The bot does not scrape Steam, Skinport, DMarket, Pricempire, CSFloat, or any
  other external marketplace during a Discord reply.
- The bot does not silently invent prices, premiums, floats, seeds, stickers, or
  liquidity signals. Unknown data should be stated as unknown.
- Passive Discord message logging is out of scope. Store only directly addressed
  bot interactions needed for product behavior, auditing, or usage accounting.
- Payment integration, user account management, multi-game support, and
  externally calibrated per-asset repricing are outside the current product
  boundary unless a specific implementation goal says otherwise.

## Definition Of Done

- The code path is implemented and covered by focused tests.
- `uv run ruff check .` and `uv run pytest` pass locally.
- Any required migration has been applied and verified.
- Docker services that need rebuilt images have been rebuilt.
- Public docs or ADRs are updated when behavior, architecture, or product
  semantics change.
