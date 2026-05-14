# skin-market

A locally-hosted CS2 skin market data aggregation service with a Discord-native LLM frontend. v1 tracks **48 hand-curated CS2 items** across **Steam Market**, **Skinport**, and **DMarket** every 15–60 minutes, computes cross-source analytics (moving averages, divergence z-scores, volume anomalies, nightly LLM narrative summaries), exposes the data through a **FastAPI read API**, and answers questions in Discord via a **bot routed through local Ollama** for tool calling. Built for an active skin trader who wants reliable, denomination-aware price intelligence without sending data to a third party.

## Architecture

```
HOST (Spark — Ubuntu, Docker, Ollama at :11434)
│
├── ollama
│   ├── huihui_ai/Qwen3.6-abliterated:27b  ── bot tool-call routing
│   └── analytics narrative model          ── nightly English summaries
│
└── docker compose stack:

    ┌── collectors ────────────────────► writes ┐
    │   Steam, Skinport, DMarket                │
    │   per-source intervals from sources       │
    │                                           ▼
    ├── analytics ─────────── reads ──► Postgres + TimescaleDB
    │   hourly: MAs, cross_source_view/spread,  ▲
    │           cross_source_divergence,        │
    │           volume_anomaly, unavailability_streak
    │   nightly (02:00 UTC): daily_narrative
    │                                           │
    ├── api ──── reads ───────────────────────── ┘
    │   FastAPI, bearer-token auth (multi-token)
    │   /items, /price, /history, /insights,
    │   /chart (PNG), /deals/evaluate
    │
    └── bot ──── http://api:8000 ──┐
        discord.py + ollama         │
        │                           │
        │ on @-mention or DM:       │
        │  1. parse via Ollama      │
        │  2. tool-call (up to 5)   │
        │  3. render reply  ────────┘
        ▼
        Discord (DM + @-mention only)
```

Outbound traffic only from the compose stack: collectors hit Steam/Skinport/DMarket; analytics + bot hit the host's Ollama. The bot also reaches Discord. Nothing reaches in except the bot's own gateway connection.

The architectural invariant: **prices from different sources are denominated differently and are never collapsed.** Steam Market quotes in Wallet Credit (~30–50% structural premium over USD because it can't be withdrawn). Skinport and DMarket quote in real-money USD. Every API response carries the `denomination` tag; the bot renders `$X.XX USD` for USD and `X.XX SC` for wallet credit. See `docs/sources-and-semantics.md`.

## Running it

### Prerequisites

- Docker + Docker Compose (recent enough for compose v2 syntax)
- Ollama daemon on the host with the bot's model pulled:
  ```bash
  ollama pull huihui_ai/Qwen3.6-abliterated:27b
  ollama serve  # or systemd unit
  ```
- A Discord application + bot token from <https://discord.com/developers/applications> with the **MESSAGE CONTENT** intent enabled (see `bot/README.md` for the walkthrough).
- A `.env` file copied from `.env.example` with real values filled in.

### Canonical bring-up

```bash
git clone <repo> ~/skin-market
cd ~/skin-market
cp .env.example .env
# Edit .env. At minimum:
#   POSTGRES_PASSWORD       (any string; only matters on first volume init)
#   SKIN_MARKET_API_TOKEN   (run: openssl rand -hex 32)
#   DISCORD_BOT_TOKEN       (from the Discord dev portal)
#   DISCORD_ALLOWED_USER_IDS (your Discord user ID; empty = reject all)

docker compose up -d
docker compose ps
# All services should be Up; api should be (healthy) after ~10s.

# Smoke-check the read API from the host (Phase 6.6 port mapping is
# 127.0.0.1:8001:8000 — host port 8001 because 8000 was held by another
# container on this deployment).
TOKEN=$(grep ^SKIN_MARKET_API_TOKEN= .env | cut -d= -f2)
curl -sS http://localhost:8001/health                  # 200, no token needed
curl -sS -H "Authorization: Bearer $TOKEN" \
  http://localhost:8001/items | jq 'length'             # 48
```

In Discord, @-mention the bot in a server the bot has been invited to:

```
@skin-market what's the AK Redline FT price?
```

If the bot is healthy you'll see a "typing…" indicator and a per-source price snapshot in 5–60 seconds (first call after Ollama idle is slower; subsequent calls are fast). If you get "I'm not authorized to chat with you", your Discord user ID isn't in `DISCORD_ALLOWED_USER_IDS` — add it and `docker compose up -d bot`.

### Stopping

```bash
docker compose stop           # graceful; collector + analytics drain in-flight cycles
docker compose down           # tear down containers; keeps data volume
docker compose down -v        # DESTRUCTIVE — drops the Postgres volume + all prices/insights
```

The `stop_grace_period` is 5 min on the collector + analytics services (matches Steam's per-cycle runtime). A `stop` during a running cycle takes that long before SIGKILL.

## Project structure

| Directory | What's there |
|---|---|
| `collectors/` | One module per upstream (Steam, Skinport, DMarket) + `scheduler.py` (APScheduler-driven, DB-aware enabled flag, retry-after honoring). Each collector returns `PriceObservation`, `DECLINED`, or `None`; the scheduler counts outcomes per cycle. |
| `analytics/` | Hourly compute jobs (moving averages, cross-source views + spreads, divergence/volume anomalies, item unavailability streaks) + nightly narrative LLM job. |
| `api/` | FastAPI read-only service. `auth.py` (bearer middleware), `schemas.py` (Pydantic v2 with `MoneyStr`), `routes/` (items, history, insights, charts, deals). |
| `bot/` | Discord bot. `main.py` (discord.py entrypoint), `ollama_client.py` (tool-use loop), `tools.py` (7 HTTP wrappers + size-discipline summarizers), `system_prompt.py`, `discord_render.py` (allowlist + attachments), `README.md` (operator install). |
| `db/` | SQLAlchemy models + Alembic migrations + connection plumbing. |
| `scripts/` | One-off operator tools — `seed_watchlist.py`, `watchlist_edit.py`. |
| `data/` | `watchlist.yaml` — the canonical 48-item list, plus source definitions. Edited via `scripts/watchlist_edit.py` so comments + ordering are preserved. |
| `docs/adr/` | Architecture Decision Records, chronological. Read these before changing anything load-bearing. |
| `docs/operations.md` | Runbooks: bring-up, image-rebuild discipline, token rotation, common failure modes. |
| `docs/sources-and-semantics.md` | The denomination invariant — Steam wallet credit vs real-money USD, why we never average across sources, why three sources beat two. |
| `tests/` | pytest. Default run skips destructive tests (those drop tables); `pytest -m destructive` opts in. 251+ tests at v1 close. |
| `bot_skill/` | **Archived** (now at `docs/archive/bot_skill_hermes_attempt/`). Phase 7b Hermes-shaped attempt; superseded by `bot/`. |

## Where to read more

1. **`docs/adr/`** — chronological tour. The headline decisions:
   - 002 — TimescaleDB over vanilla Postgres
   - 006 — Collector resilience (retry/backoff strategy)
   - 009 — Scheduler design (overlap policy, conditional writes, SIGTERM)
   - 013 — Rate-limit policy (DB-driven scheduler, Retry-After honoring, declined vs unavailable split, observation_log for streaks)
   - 014 — Read API design (money-as-string, denomination tagging, auth multi-token, `/health` bypass)
   - 015 — Bot skill design (rules; the Hermes runtime was retired by ADR 016 but the rendering rules carried forward)
   - 016 — Bot runtime (Ollama Default-not-Native, tool-use loop, size discipline, defensive failure modes)
2. **`docs/operations.md`** — what to type when something is broken. Image-rebuild discipline is the most common foot-trap.
3. **`docs/sources-and-semantics.md`** — why averaging is a category error in this domain.

## Tests

```bash
uv run pytest          # default; skips destructive tests
uv run pytest -m destructive   # opts in; wipes tables on a throwaway dev DB
uv run ruff check .    # linter
```

## License

Proprietary. Not licensed for redistribution.
