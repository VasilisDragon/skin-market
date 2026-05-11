# PROJECT_SPEC.md — v1 implementation plan

Read this together with ARCHITECTURE.md before writing any code. ARCHITECTURE.md is the *how*; this is the *what and in what order*.

## v1 scope

A working CS2 skin market data pipeline + Discord bot frontend, covering:

- Two data sources: Steam Community Market `priceoverview` and Skinport public API
- A starter watchlist of ~50 items (popular knives, gloves, rifles, pistols, AWPs)
- Postgres with TimescaleDB for time-series storage
- Basic analytics: current price, 7-day average, 30-day average, 7-day high/low, % change
- FastAPI service exposing read-only endpoints
- Hermes Discord skill with tools to query the API
- Chart rendering (matplotlib, served as PNG)
- Docker Compose for local-on-Spark deployment

Out of scope (per ARCHITECTURE.md): float pricing, multi-game, news layer, web UI, auth, payments.

## Build order (each phase has explicit acceptance criteria)

### Phase 0 — Repo scaffold and dev environment

**Deliverables:**
- `pyproject.toml` with deps locked (use `uv` for dependency management — fast, modern, lockfile-based)
- `docker-compose.yml` with one service for now: `postgres` (TimescaleDB image)
- `.env.example` with all expected env vars documented
- Directory structure per ARCHITECTURE.md
- `README.md` with "how to dev locally" section
- `.gitignore` (Python + IDE + .env)

**Acceptance:**
```bash
git clone <repo>
cp .env.example .env
docker compose up -d postgres
uv sync
uv run python -c "from db.connection import get_engine; print(get_engine())"
# prints a working engine, no errors
```

### Phase 1 — Database schema and migrations

**Deliverables:**
- SQLAlchemy models for: `items`, `prices` (hypertable), `sources`, `insights`
- Alembic configured for migrations
- Initial migration creating all tables + the TimescaleDB hypertable conversion
- Connection module that reads from `DATABASE_URL` env var
- A seed script populating ~50 CS2 items in the `items` table

**Schema sketch:**

```sql
CREATE TABLE items (
    id UUID PRIMARY KEY,
    market_hash_name TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    item_type TEXT,           -- 'knife', 'glove', 'rifle', etc.
    weapon_name TEXT,         -- 'AK-47', 'AWP', etc.
    skin_name TEXT,
    wear TEXT,                -- 'Factory New', etc.
    is_stattrak BOOLEAN DEFAULT FALSE,
    is_souvenir BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE sources (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,   -- 'steam_market', 'skinport'
    base_url TEXT,
    rate_limit_per_minute INTEGER,
    enabled BOOLEAN DEFAULT TRUE
);

CREATE TABLE prices (
    item_id UUID NOT NULL REFERENCES items(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    timestamp TIMESTAMPTZ NOT NULL,
    price NUMERIC(12,2) NOT NULL,
    volume INTEGER,           -- 24h volume where available
    currency TEXT DEFAULT 'USD',
    raw_response JSONB,       -- keep the original for debugging
    PRIMARY KEY (item_id, source_id, timestamp)
);
SELECT create_hypertable('prices', 'timestamp');

CREATE TABLE insights (
    id BIGSERIAL PRIMARY KEY,
    item_id UUID NOT NULL REFERENCES items(id),
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    insight_type TEXT NOT NULL,  -- 'moving_avg_7d', 'volume_anomaly', etc.
    value NUMERIC,
    metadata JSONB
);
CREATE INDEX ON insights (item_id, insight_type, computed_at DESC);
```

**Acceptance:**
- `alembic upgrade head` runs cleanly
- The seed script populates items
- A test that inserts a price row and queries it back

### Phase 2 — Steam Market collector

**Deliverables:**
- `collectors/base.py` with abstract `Collector` interface
- `collectors/steam.py` implementing the Steam Market `priceoverview` integration
- Conservative rate limiting: 50 items per cycle, 30-minute cycles, 1 request per 5 seconds, exponential backoff on 429
- Tests with `pytest-httpx` mocking Steam responses, including 429 retry logic
- Logging that goes to stdout with structured JSON

**Steam API details:**
- Endpoint: `https://steamcommunity.com/market/priceoverview/?country=US&currency=1&appid=730&market_hash_name=<urlencoded>`
- Returns JSON: `{"success":true,"lowest_price":"$X","median_price":"$Y","volume":"N"}`
- Use a realistic User-Agent (Chrome 130, not Python's default)
- Steam blocks repeated requests without a session cookie eventually — note this in code comments; user will provide one later if needed

**Acceptance:**
```bash
uv run python -m collectors.steam --item "AK-47 | Redline (Field-Tested)"
# fetches once, prints normalized result, writes to DB
```

### Phase 3 — Skinport collector

**Deliverables:**
- `collectors/skinport.py` implementing the Skinport public API
- Endpoint: `https://api.skinport.com/v1/items?app_id=730&currency=USD`
- This returns ALL Skinport items in one call — cache it, don't refetch per item
- Cycle: every 5 minutes (Skinport is permissive)
- Tests

**Acceptance:**
```bash
uv run python -m collectors.skinport
# fetches once, writes prices for all watchlist items that exist on Skinport
```

### Phase 4 — Scheduler service

**Deliverables:**
- `collectors/scheduler.py` using APScheduler
- Schedules: Steam every 30min (50 items/cycle, rotating through watchlist), Skinport every 5min
- A `docker-compose.yml` service `collector` that runs the scheduler
- Graceful shutdown on SIGTERM
- Logs visible via `docker compose logs collector`

**Acceptance:**
- `docker compose up -d collector` runs cleanly
- `docker compose logs --tail 50 collector` shows successful collection cycles
- New rows in `prices` table after a few minutes

### Phase 5 — Analytics jobs

**Deliverables:**
- `analytics/moving_averages.py` computing 7-day and 30-day MAs per item
- `analytics/anomaly_detection.py` flagging volume anomalies (>2 stddev from rolling mean)
- `analytics/jobs.py` as the cron entry point
- Cron schedule: every 1 hour, computes insights and writes to `insights` table
- Tests

**Acceptance:**
- After running collection for >24h, running the analytics job produces rows in `insights`
- A query like `SELECT * FROM insights WHERE item_id = X AND insight_type = 'moving_avg_7d' ORDER BY computed_at DESC LIMIT 1` returns a sensible number

### Phase 6 — FastAPI service

**Deliverables:**
- `api/main.py` FastAPI app
- Endpoints:
  - `GET /items` — list watchlist
  - `GET /items/{slug}` — item details
  - `GET /items/{slug}/price` — current best price across sources + per-source breakdown
  - `GET /items/{slug}/history?days=30&source=steam` — price history
  - `GET /items/{slug}/insights` — latest analytics
  - `GET /items/{slug}/chart?days=30&source=all` — PNG chart
  - `POST /deals/evaluate` — body: `{slug, price, currency}` — returns `{verdict, reasoning, comparable_prices}`
- Pydantic models for all request/response shapes
- OpenAPI auto-generated at `/docs`
- Tests with `pytest` + `httpx.AsyncClient`

**Acceptance:**
```bash
curl localhost:8000/items/ak47-redline-fn/price | jq
# returns structured price data

curl localhost:8000/items/ak47-redline-fn/chart?days=30 > chart.png
# valid PNG
```

### Phase 7 — Hermes Discord skill

**Deliverables:**
- `bot_skill/SKILL.md` describing the skill to Hermes
- `bot_skill/tools.py` implementing tools: `query_current_price`, `query_price_history`, `render_chart`, `evaluate_deal`, `list_watchlist`
- Skill installed into `~/.hermes-discord/skills/skin-market/`
- Tools make HTTP calls to the FastAPI service (treat it as external)

**Acceptance:**
- In Discord: `@bot what's the current price of AK Redline FN?` → bot replies with price + source breakdown
- `@bot show me a 30-day chart for AK Redline FN` → bot uploads PNG chart
- `@bot is $25 a good price for AK Redline FN?` → bot calls evaluate_deal, replies with verdict

### Phase 8 — Docker Compose for the full stack

**Deliverables:**
- `docker-compose.yml` with services: `postgres`, `collector`, `analytics` (cron), `api`
- Healthchecks on postgres
- `depends_on` with healthcheck conditions so collectors don't start until DB is ready
- `restart: unless-stopped` on all services
- Networking: only `api` exposes a port to the host (8000); everything else is on the internal network
- A README section "Deploy to Spark" with the exact steps

**Acceptance:**
```bash
# On the Spark, in the cloned repo:
cp .env.example .env
# edit .env
docker compose up -d
docker compose ps
# all 4 services healthy
curl localhost:8000/items/ak47-redline-fn/price
# returns data within a few minutes of startup
```

## Implementation order priority

If you have to pause partway through, the right stopping points are:

- After phase 1: schema is solid foundation
- After phase 2 or 3: at least one collector working, data flowing
- After phase 6: API is queryable, even if bot integration isn't done
- After phase 7: end-to-end works in a manual-start state

Phase 8 (Docker compose for full stack) is the polish that makes it deployable; everything before it can run locally with `uv run`.

## What I want you (Claude Code) to do first

1. Read ARCHITECTURE.md and this file completely
2. Confirm the plan back to the user in 3-5 sentences: what you understand the scope to be, what you'll build first, and any concerns or questions
3. **Wait for user approval** before starting Phase 0. Don't just dive in.
4. Then build Phase 0, commit, and pause for review before Phase 1.

After phase 0 is reviewed and approved, you may proceed phase-by-phase, committing at each phase boundary and pausing for review between. Don't batch phases.
