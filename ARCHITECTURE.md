# ARCHITECTURE.md — skin-market

## What this project is, in one sentence

A locally-hosted, eventually-public CS2 skin market data aggregation service with a Discord-native LLM frontend, designed to compete with Pricempire and CSFloat on Discord UX and AI-powered deal evaluation.

## Critical workflow rules

**Before debugging a symptom, state in one sentence what the app is supposed to do in that state. If you can't, that's the first thing to figure out.**

**You cannot see screenshots from the user's chat with the upstream Claude.** Prefer text-based diagnostics — console output, curl results, computed values, SQL query results — over screenshot references in prompts you write back. If you need to see something visual, write code to print it as text or save it to a file the user can describe to you.

**Architecture decisions get committed to ADR files.** When you make a non-obvious choice (library selection, schema shape, API design), drop a one-page `docs/adr/NNN-title.md` explaining what you picked, what you rejected, and why. Future-you will thank present-you.

**No "magic" libraries.** Prefer boring, well-documented tools the user can debug at 2am with a Stack Overflow search: `httpx`, `psycopg` (v3), `sqlalchemy`, `fastapi`, `apscheduler`, `matplotlib`, `pandas`. Skip exotic frameworks even if they look slicker.

**Tests for non-trivial logic.** Pure data-shuffling doesn't need tests. Pricing math, deal evaluation, schema migrations, anything that has a "right answer" — test it. `pytest` is the default.

**The user's background is infrastructure / systems engineering, not Python.** Code should be readable by someone who knows infrastructure but not the latest Python idioms. Type hints, yes. Docstrings on public functions, yes. Clever metaclass tricks, no.

**Schema-dependent code must not be merged before its migration is applied.** Code that reads or writes a table cannot be on `main` ahead of the migration that creates that table — in every environment where the code runs. In this single-environment dev setup, that collapses to "apply the migration first, then verify the code." For additive migrations bundled with code-using-the-new-schema in one commit, the verification gate is: dry-run the migration SQL → apply migration → verify the expected outcome (row count, schema shape) → run code-level tests → confirm green. Tests-then-migration is the rule for non-additive changes (column drops, type narrowing, anything that could break running readers). Pin the lesson learned during Phase 2b Step 2: the literal rule "tests pass before alembic upgrade" is impossible to satisfy when an additive migration and a code change that depends on it land together — the table must exist for the new code path to succeed.

**Pre-push verification must run BOTH `uv run ruff check .` AND `uv run pytest`, not just pytest.** CI runs `ruff check` before `pytest`; a lint failure blocks the test step entirely, so local pytest-passing is necessary but not sufficient for CI green. Phase 2b's Step 7.2 push surfaced this: 4 commits landed on `main` with 25 ruff errors that pytest didn't catch (the test bodies themselves passed, but ruff flagged long lines, unused loop variables, and `if`/`else`-could-be-ternary patterns). Both Python 3.11 and 3.12 CI jobs went red. The fix was mechanical but the push was wasted. From now on: `uv run ruff check . && uv run pytest` before every `git push`.

**Restart vs rebuild discipline for Docker services.** Code changes baked into the image require `docker compose up -d --build SERVICE`. Env-var-only changes (read at runtime) require `docker compose restart SERVICE`. Audit which is which BEFORE flipping. Phase 2b's Step 7.2 Gate 1 (collector) and Gate 2 (analytics) both initially hit the under-built-image trap because running containers were 2+ days stale relative to the working tree's code. `docker compose restart` is the wrong tool when the service has accumulated unbuild code changes since its current image.

## The architecture, briefly

```
   Steam   Skinport   DMarket   Pricempire
     │        │          │          │
     └────────┴─────┬────┴──────────┘   HTTPS
                    ▼
┌────────────────────── DGX Spark (Ubuntu) ─────────────────────┐
│ ┌──── docker compose ───────────────────────────────────────┐ │
│ │  ┌─────────────┐   ┌─────────────┐   ┌─────────────────┐  │ │
│ │  │ collector   │──▶│  postgres   │◀──│ analytics       │  │ │
│ │  │ (scheduler) │   │ (timescale) │   │ (hourly+daily)  │  │ │
│ │  └─────────────┘   └─────────────┘   └─────────────────┘  │ │
│ │                           ▲                               │ │
│ │                   ┌───────┴───────┐                       │ │
│ │                   │ api (uvicorn, │                       │ │
│ │                   │ read-only)    │                       │ │
│ │                   └───────▲───────┘                       │ │
│ │                           │ HTTP + bearer                 │ │
│ │                   ┌───────┴───────┐                       │ │
│ │                   │ bot           │                       │ │
│ │                   │ (discord.py)  │                       │ │
│ │                   └───┬───────┬───┘                       │ │
│ └───────────────────────┼───────┼───────────────────────────┘ │
│                         │       │ Ollama HTTP                 │
│                         │       ▼                             │
│                         │  ┌─────────────────────────────┐    │
│                         │  │ Ollama (host process)       │    │
│                         │  │ host.docker.internal:11434  │    │
│                         │  │ huihui_ai/Qwen3.6-abliter.. │    │
│                         │  └─────────────────────────────┘    │
└─────────────────────────┼─────────────────────────────────────┘
                          ▼
                     Discord (outbound)
```

Layers and their responsibilities:

- **Collectors:** Pure data ingestion. Two families:
  - **Per-item collectors** (Steam, Skinport, DMarket) — one Python module per source, one HTTP call per item per cycle, written to `prices`. Each runs on its own APScheduler-driven schedule via the `BaseCollector` abstraction. Used for the curated 48-item watchlist.
  - **Bulk-snapshot collector** (Pricempire, Phase 2a) — one Python module that pulls the entire ~39,400-item CS2 catalog in one HTTP call and writes per-provider rows to a separate `pricempire_observations` hypertable. Does NOT extend `BaseCollector`; ADR 018/019 document why. Provides breadth coverage layered on top of the curated direct-poll collectors.
  Both families share no business logic; just fetch, normalize, write.
- **Database (Postgres + TimescaleDB extension):** Source of truth. Hypertables for time-series price data, regular tables for items and metadata.
- **Analytics jobs:** Cron-triggered Python that computes derived data — moving averages, volume anomalies, price velocity, news-correlated moves. Output to `insights` tables.
- **FastAPI app:** Read-only API. The bot doesn't touch Postgres directly; it goes through this layer. This is also the future SaaS API surface.
- **Bot:** A `discord.py` event loop running as a compose service (Phase 7c, ADR 016). Reads from the FastAPI app only — never touches Postgres directly, never scrapes upstreams. Tool functions (`list_watchlist`, `query_current_price`, `query_price_history`, `render_chart`, `evaluate_deal`, `narrative_today`, `whats_interesting`) are thin `httpx` wrappers over the read API; the LLM chooses which to call.

The LLM only enters the picture in two places:
1. The bot calls local Ollama via `ollama.AsyncClient.chat(model=…, tools=TOOL_DEFINITIONS)` — i.e. the standard chat-completion endpoint with `tools=[…]` in the request payload. This is the **Default** tool-calling path, NOT Ollama's **Native** variant; ADR 016 documents the choice as load-bearing for the `huihui_ai/Qwen3.6-abliterated:27b` model in use.
2. The analytics narrative job uses the same Ollama instance nightly at 02:00 UTC to generate a one-paragraph market summary (enrichment path, async).

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
