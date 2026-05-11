# ARCHITECTURE.md — skin-market

## What this project is, in one sentence

A locally-hosted, eventually-public CS2 skin market data aggregation service with a Discord-native LLM frontend, designed to compete with Pricempire and CSFloat on Discord UX and AI-powered deal evaluation.

## Critical workflow rules

**Before debugging a symptom, state in one sentence what the app is supposed to do in that state. If you can't, that's the first thing to figure out.**

**You cannot see screenshots from the user's chat with the upstream Claude.** Prefer text-based diagnostics — console output, curl results, computed values, SQL query results — over screenshot references in prompts you write back. If you need to see something visual, write code to print it as text or save it to a file the user can describe to you.

**Architecture decisions get committed to ADR files.** When you make a non-obvious choice (library selection, schema shape, API design), drop a one-page `docs/adr/NNN-title.md` explaining what you picked, what you rejected, and why. Future-you will thank present-you.

**No "magic" libraries.** Prefer boring, well-documented tools the user can debug at 2am with a Stack Overflow search: `httpx`, `psycopg` (v3), `sqlalchemy`, `fastapi`, `apscheduler`, `matplotlib`, `pandas`. Skip exotic frameworks even if they look slicker.

**Tests for non-trivial logic.** Pure data-shuffling doesn't need tests. Pricing math, deal evaluation, schema migrations, anything that has a "right answer" — test it. `pytest` is the default.

**The user is a Systems Engineer at an MSP, not a Python expert.** Code should be readable by someone who knows infrastructure but not the latest Python idioms. Type hints, yes. Docstrings on public functions, yes. Clever metaclass tricks, no.

## The architecture, briefly

```
┌────────────────────────────────────────────────────────────────┐
│                       DGX Spark (Ubuntu)                       │
│                                                                │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────────┐  │
│  │ Collectors  │─▶│  Postgres   │◀─│ Analytics jobs (cron)  │  │
│  │ (poll APIs) │  │  (timescale)│  │ Pre-compute insights   │  │
│  └─────────────┘  └─────────────┘  └────────────────────────┘  │
│         │                │                       │             │
│         │                ▼                       │             │
│         │       ┌─────────────────┐              │             │
│         └──────▶│  FastAPI app    │◀─────────────┘             │
│                 │  (read-only)    │                            │
│                 └─────────────────┘                            │
│                          ▲                                     │
│                          │ HTTP                                │
│                          │                                     │
│                 ┌─────────────────┐                            │
│                 │ Hermes Discord  │ ─── calls skill ──┐        │
│                 │ bot (skill)     │                   │        │
│                 └─────────────────┘                   ▼        │
│                                          ┌──────────────────┐  │
│                                          │ Ollama (existing)│  │
│                                          │ qwen3-coder, etc │  │
│                                          └──────────────────┘  │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                     Discord users
```

Layers and their responsibilities:

- **Collectors:** Pure data ingestion. One Python module per source (Steam, Skinport, CSFloat later). Each runs on its own schedule via APScheduler. No business logic; just fetch, normalize, write.
- **Database (Postgres + TimescaleDB extension):** Source of truth. Hypertables for time-series price data, regular tables for items and metadata.
- **Analytics jobs:** Cron-triggered Python that computes derived data — moving averages, volume anomalies, price velocity, news-correlated moves. Output to `insights` tables.
- **FastAPI app:** Read-only API. The bot doesn't touch Postgres directly; it goes through this layer. This is also the future SaaS API surface.
- **Hermes skill:** A skill bundle (SKILL.md + tool definitions) that gives the Discord bot capabilities like `query_current_price(item)`, `query_price_history(item, days)`, `render_chart(item, days)`, `evaluate_deal(item, float, price)`.

The LLM only enters the picture in two places:
1. The Hermes bot parses the user's Discord message and decides which skill tools to call (query path)
2. The analytics jobs occasionally use Ollama to generate market commentary from news + price data (enrichment path, async)

It does NOT do data fetching, scraping, parsing, or math. Those are deterministic Python.

## Repo layout

```
skin-market/
├── ARCHITECTURE.md                  # this file
├── README.md                  # human-readable overview
├── PROJECT_SPEC.md            # what to build, in order
├── docker-compose.yml         # orchestrates all services
├── .env.example
├── pyproject.toml
├── docs/
│   └── adr/                   # architecture decision records
├── collectors/
│   ├── __init__.py
│   ├── base.py                # shared collector interface
│   ├── steam.py
│   ├── skinport.py
│   └── scheduler.py           # APScheduler entry point
├── db/
│   ├── __init__.py
│   ├── models.py              # SQLAlchemy models
│   ├── migrations/            # Alembic migrations
│   └── connection.py
├── analytics/
│   ├── __init__.py
│   ├── moving_averages.py
│   ├── anomaly_detection.py
│   └── jobs.py                # cron entry points
├── api/
│   ├── __init__.py
│   ├── main.py                # FastAPI app
│   ├── routes/
│   │   ├── prices.py
│   │   ├── history.py
│   │   ├── charts.py
│   │   └── deals.py
│   └── schemas.py             # Pydantic models
├── bot_skill/
│   ├── SKILL.md               # for Hermes
│   └── tools.py
├── tests/
│   ├── test_collectors.py
│   ├── test_analytics.py
│   └── test_api.py
└── scripts/
    └── seed_watchlist.py      # initial item list
```

## Anti-patterns to specifically avoid

- **Don't have the bot scrape live.** All data goes through the collector → DB → API path. If the bot tries to bypass and curl Steam directly, that's a bug.
- **Don't over-poll Steam.** They will block you. Maximum 50 items per polling cycle, 30-minute cycles, exponential backoff on 429s. Skinport is more permissive.
- **Don't use floats for money.** PostgreSQL `NUMERIC(12,2)` for all prices. Python `Decimal` for math.
- **Don't trust item name strings as primary keys.** Steam's `market_hash_name` is canonical-ish but has edge cases (StatTrak™ has a unicode TM, the spaces matter). Use a normalized slug + the raw name + a UUID PK.
- **Don't catch generic exceptions.** Catch specific ones (`httpx.TimeoutException`, `httpx.HTTPStatusError`). A bare `except:` swallowing bugs is the source of half of all production incidents.
- **Don't put secrets in code or commit them.** `.env` file, never committed. The Discord bot token, the CSFloat API key (later), the PostgreSQL password — all environment variables.

## Fatigue monitoring during long sessions

During multi-session relayed debugging, watch for these signals from the user:
- Repeating an already-answered question
- Sending data that doesn't match the ask
- Confusing which bug/session is active
- Losing track of whether a fix was tested vs just written
- Apologizing for "screwing up chains"

Two or more signals stacking — gently suggest a break. The user has explicitly requested this monitoring.

## When the user shares others' messages

Assess substance and register separately. Informal tone isn't evidence of shallow thinking. Identify what facts would distinguish charitable from uncharitable reads; if unavailable, reserve judgment and ask rather than defaulting to the rhetorically rewarding interpretation.

## Definition of done for each phase

A phase is done when:
1. All listed deliverables are present in the repo
2. Tests pass (`pytest`)
3. `docker compose up -d` brings the relevant services up cleanly
4. There's a short demo command or curl in `README.md` showing it works
5. An ADR is committed for any architectural decision that wasn't pre-specified

## Things explicitly out of scope for v1

- Float-tier pricing (CSFloat integration is v2)
- Multi-game support (v4)
- News/speculation layer (v3)
- Web frontend (v5)
- User accounts / auth (v5+)
- Payment integration (v5+)
- Real-time websocket pricing (v6+ if ever)
- **Passive message logging.** The bot does NOT log non-addressed Discord
  messages to any database. Discord's developer ToS restricts this, and the
  privacy story doesn't work for a future paid product. The bot stores only
  messages it was directly addressed in (via @-mention or slash command).
- **Autonomous prediction generation.** The bot does NOT generate market
  predictions on its own initiative based on chat content. Predictions,
  when they exist (post-v1), are explicitly user-triggered (e.g. a
  `/predict` command).
- **Self-scheduled prediction validation loops.** No cron jobs that grade
  past predictions against actual prices in v1. The infrastructure for
  this is post-v1.
- **LLM-generated markdown knowledge files.** The bot does NOT write
  summary `.md` files that it later reads as a poor-man's memory layer.
  Structured storage (Postgres tables) is the canonical knowledge source;
  drift between LLM-written summaries and underlying data is a known
  anti-pattern we're avoiding by construction.
- **Live external API calls from the bot's reply path.** The bot reads
  from the local Postgres only. If an item isn't in our DB, the correct
  response is "I don't track that item — request it be added to the
  watchlist." Never have the bot scrape Steam/Skinport during a Discord
  conversation.
