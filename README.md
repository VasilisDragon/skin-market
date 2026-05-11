# skin-market

A locally-hosted, eventually-public CS2 skin market data aggregation service with a Discord-native LLM frontend.

## What this does

Polls multiple skin marketplaces on a schedule, stores the data, computes analytics, and exposes the results both as a REST API and as a Hermes Agent Discord bot. The bot can answer questions like:

- "What's the current best price for AK Redline FN?"
- "Show me a 30-day price chart for Karambit Doppler"
- "Is $400 a good price for a Glock Fade with 0.07 float?" (v2 once CSFloat is integrated)
- "Which items had unusual volume in the last 24 hours?"

## Tech stack

- **Python 3.11+** with `uv` for dependency management
- **PostgreSQL + TimescaleDB** for time-series storage
- **APScheduler** for collector scheduling
- **FastAPI** for the read API
- **matplotlib** for chart rendering
- **Hermes Agent** for the Discord frontend (skill bundle in `bot_skill/`)
- **Docker Compose** for orchestration

Everything runs on a single DGX Spark via Docker Compose. No cloud services required for v1.

## Status

Project is in v1 development. Read `PROJECT_SPEC.md` for what's being built and `ARCHITECTURE.md` for the workflow.

## Architecture

See `ARCHITECTURE.md` for the diagram and rationale. Briefly:

```
Collectors → Postgres → Analytics → API → Hermes Bot → Discord
```

The LLM (Hermes/Ollama) does NOT scrape, fetch, or compute. It parses user questions, calls deterministic tools, and writes the response.

## Getting started (development)

Prereqs: Docker, `uv` installed, Postgres client (`psql`) for debugging.

```bash
# Clone the repo
git clone <your-fork> skin-market
cd skin-market

# Set up environment
cp .env.example .env
# edit .env — set DATABASE_URL, etc.

# Start postgres
docker compose up -d postgres

# Install Python deps
uv sync

# Run migrations
uv run alembic upgrade head

# Seed the watchlist
uv run python scripts/seed_watchlist.py

# Run a one-shot collection (for testing)
uv run python -m collectors.steam --item "AK-47 | Redline (Field-Tested)"

# Start the API
uv run uvicorn api.main:app --reload --port 8000

# In another terminal, hit the API
curl localhost:8000/items/ak47-redline-fn/price | jq
```

## Deploy to Spark (production)

Once Phase 8 of `PROJECT_SPEC.md` is done:

```bash
# On the Spark
ssh vasilis@<spark-ip>
git clone <repo> ~/skin-market
cd ~/skin-market
cp .env.example .env
# edit .env with prod values
docker compose up -d
docker compose ps
# all services healthy
```

## Connecting the Hermes Discord bot

The bot lives in a separate Hermes home (`~/.hermes-discord/`). The skill bundle gets installed there:

```bash
mkdir -p ~/.hermes-discord/skills/
cp -r bot_skill/ ~/.hermes-discord/skills/skin-market/
# Restart hermes gateway
```

The skill calls the API at `http://localhost:8000` (when the bot runs on the same Spark) or `http://<spark-ip>:8000` (if the bot runs elsewhere).

## Roadmap

- **v1** (current) — Steam + Skinport, ~50 items, basic analytics, Discord bot
- **v2** — Add CSFloat for float-tier pricing, deal evaluation accounts for float bands
- **v3** — News/speculation layer (RSS ingestion, LLM commentary on price moves)
- **v4** — Multi-game (Dota 2, TF2, Rust)
- **v5** — Web frontend, user accounts, paid tiers
- **vN (post-v3, exact phase TBD)** — Autonomous predictor / listener / validation loop. Per-server opt-in, strict per-server data isolation. Paid-tier feature; see PROJECT_SPEC.md "Post-v1 roadmap" for the full picture.

## License

TBD.
