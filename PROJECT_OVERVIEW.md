# skin-market — Project Overview

> Regenerated 2026-05-16 from a fresh codebase audit. Supersedes the prior file (which dated to ~2026-05-15 and never had a full re-audit during Phase 2a). Every claim below is grounded in a file actually read during this pass; no statements were carried forward unverified. Where the audit found something the older overview got wrong, the current text quietly corrects it — see the summary at the bottom of the file.

---

## 1. Purpose

skin-market is a locally-hosted CS2 skin price aggregation service with a Discord-native LLM frontend. It polls public marketplace APIs (Steam Community Market, Skinport, DMarket) on a curated 48-item watchlist, layers in Pricempire's whole-catalog bulk feed for breadth coverage across six third-party providers (buff163, buff163_buy, skinport, dmarket, csmoney, swap.gg), and exposes the resulting time-series via a read-only FastAPI surface that a local-Ollama-backed Discord bot queries through tool-calling. Long-term it's positioned to compete with Pricempire/CSFloat on Discord UX and AI-powered deal evaluation; today it runs in a five-container Docker Compose stack on the operator's DGX Spark host.

---

## 2. Stack & deployment

**Host environment.** Ubuntu on the DGX Spark (aarch64). Ollama runs natively on the host at `127.0.0.1:11434`; the compose stack reaches it via `host.docker.internal:host-gateway` (`docker-compose.yml:146-147`, `:174-177`).

**Languages / runtime.** Python `>=3.11,<3.13`. Container base is `python:3.12-slim` (`Dockerfile:6`). Dependencies managed by `uv` (Astral) — `uv sync --frozen --no-dev` at image build (`Dockerfile:21-22`). One image, four entry-point variants — only the `command:` differs across the four Python services.

**Key libraries.** `fastapi` + `uvicorn[standard]` (read API), `sqlalchemy` 2.x + `psycopg[binary]` v3 (ADR 004), `alembic` (migrations), `apscheduler` (cycle scheduling), `httpx` + `brotli` (Skinport requires `Accept-Encoding: br`), `ijson` (Pricempire streaming JSON parse), `pydantic` v2, `matplotlib` (charts), `pandas`, `discord.py`, `ollama` (local-model client), `ruamel.yaml` (comment-preserving watchlist edits).

**Five containers** (`docker-compose.yml`):

| Service | Container | Image / command | Role |
|---|---|---|---|
| `postgres` | `skinmarket-postgres` | `timescale/timescaledb:2.17.2-pg16` | Sole stateful service. Bound to `127.0.0.1:5432`. Initialized with TimescaleDB extension via migration 0001. |
| `collector` | `skinmarket-collector` | `python -m collectors.scheduler` | `BlockingScheduler` running one APScheduler interval job per enabled source row. `stop_grace_period: 5m` so Steam's ~4-minute cycle drains on SIGTERM. |
| `api` | `skinmarket-api` | `uvicorn api.main:app --host 0.0.0.0 --port 8000 --proxy-headers` | Read-only API. Host port mapping `127.0.0.1:8001->8000`. Liveness via `/health`. |
| `bot` | `skinmarket-bot` | `python -m bot.main` | discord.py event loop. No exposed port (outbound only). Depends on `api` healthcheck. |
| `analytics` | `skinmarket-analytics` | `python -m analytics.scheduler` | `BlockingScheduler` with hourly + 02:00-UTC-daily jobs. `stop_grace_period: 5m` for Ollama narrative tail. |

Shared `x-logging` YAML anchor caps stdout to `50m × 5 files` per service (`docker-compose.yml:13-19`). Restart policy on every service is `unless-stopped`. The compose file's header (`:1-7`) carries an explicit footgun note: `POSTGRES_PASSWORD` is consumed only on the first volume init; later rotation requires `ALTER USER` inside the running container.

**Bringing it up.**

```
cp .env.example .env       # fill POSTGRES_PASSWORD, DISCORD_BOT_TOKEN,
                           # SKIN_MARKET_API_TOKEN, PRICEMPIRE_API_KEY,
                           # DISCORD_ALLOWED_USER_IDS, OLLAMA_MODEL
docker compose up -d
docker compose exec api alembic upgrade head     # only on first deploy
docker compose exec api python -m scripts.seed_watchlist
```

The collector / bot / analytics images all share the single Dockerfile, so `docker compose build` builds one image and the four Python services launch from it.

---

## 3. Data model

Postgres 16 with TimescaleDB 2.17.2. Alembic head: `0007`. Database name `skinmarket`. Eight application tables plus `alembic_version`.

### 3.1 Phase 1 tables (curated-collector world)

| Table | Key columns | Hypertable? | Purpose |
|---|---|---|---|
| `items` | `id UUID PK`, `market_hash_name UNIQUE`, `display_name`, `slug UNIQUE`, `item_type`, `weapon_name`, `skin_name`, `wear`, `is_stattrak`, `is_souvenir`, `created_at` | no | The watchlist registry. UUID PK so a slug rename can't break FKs. |
| `sources` | `id SERIAL PK`, `name UNIQUE`, `base_url`, `rate_limit_per_minute`, `enabled BOOL`, `denomination`, `interval_minutes`, `per_item_delay_seconds` | no | One row per upstream (real or pseudo). `denomination` is `usd` / `wallet_credit` / NULL — keeps the bot from collapsing across currencies. `interval_minutes` / `per_item_delay_seconds` carry per-source scheduling (added in migration 0003, ADR 013). |
| `prices` | composite PK `(item_id, source_id, timestamp)`; `price NUMERIC(12,2)`, `volume INT`, `currency`, `raw_response JSONB` | **yes** (7-day chunks, columnar compression on chunks ≥30d, `compress_segmentby = 'item_id, source_id'`) | Per-item-per-source observations from the curated Steam/Skinport/DMarket collectors. **Dedup-on-write** (ADR 009 §3): an observation is skipped when its `(price, volume)` exactly matches the latest existing row for the same `(item_id, source_id)`. |
| `observation_log` | composite PK `(item_id, source_id)`; `last_observed_at TIMESTAMPTZ` | no | Pre-dedup "we successfully polled this pair at time T" signal. Upserted unconditionally on every successful curated-collector poll. Drives the bot's freshness rendering and the unavailability-streak analytics. (Migration 0004, ADR 017.) |
| `insights` | `id BIGSERIAL PK`, `item_id`, `computed_at`, `insight_type TEXT`, `value NUMERIC`, `text_value TEXT`, `meta_info JSONB` | no | Derived analytics output: moving averages, cross-source view/spread, divergence + volume anomalies, unavailability streaks, daily narrative. Indexed on `(item_id, insight_type, computed_at DESC)`. |

### 3.2 Phase 2a tables (Pricempire breadth layer)

Migrations 0005 and 0007. Both kept off `prices` deliberately — ADR 018 §2 documents why (schema-shape, cardinality, provenance).

#### `pricempire_observations` (migration 0005)

Composite PK `(item_id, source_id, timestamp)`. TimescaleDB hypertable, 7-day chunks. Index on `timestamp DESC`. No compression policy in Phase 2a — deferred.

| Column | Type | Notes |
|---|---|---|
| `item_id` | UUID FK→items.id (PK) | Restricted to items already in the curated watchlist (Phase 2a ADR 018 §6); the long-tail layer is Phase 2b. |
| `source_id` | INT FK→sources.id (PK) | Points at one of the six `pricempire_*` sub-provider rows. Never points at the `pricempire` pseudo-source. |
| `timestamp` | TIMESTAMPTZ (PK) | **Local clock at row-write time.** Drives dedup, TimescaleDB chunking, and the project-canonical "when did we record this" semantics. *Not* a poll-freshness signal — see §8. |
| `price` | NUMERIC(12,2) | USD, converted from Pricempire's wire-cents (`Decimal(str(raw)) / 100`). |
| `count` | INT | Per-provider listings count. Distinct from `prices.volume`. |
| `updated_at` | TIMESTAMPTZ | Pricempire's claim of when the underlying price last moved. **Skinport rows carry a `2025-01-01T00:00:00Z` placeholder; swap.gg rows carry it ~29% of the time.** Drift logic must ignore this and use `last_checked_at`. |
| `last_checked_at` | TIMESTAMPTZ | Pricempire's claim of when it polled the provider. The honest "is Pricempire still refreshing?" signal. |
| `currency` | VARCHAR(8), default `'USD'` | |
| `raw_response` | JSONB | The full per-provider wire row. |

The three timestamps are intentional (ADR 018 §4, ADR 019 §1): Phase 1 (ADR 017) taught the project to separate "when we wrote it" from "when the source polled it" from "when the price moved." Pricempire surfaces the last two as its own assertions, so we store all three and never collapse them.

#### `pricempire_item_metadata` (migration 0007)

Composite PK `(item_id, timestamp)`. TimescaleDB hypertable, 7-day chunks. Index on `(item_id, timestamp DESC)`. Per-item, not per-provider — so `source_id` is not part of the key. ADR 020.

| Column | Type | Notes |
|---|---|---|
| `item_id` | UUID FK→items.id (PK) | |
| `timestamp` | TIMESTAMPTZ (PK) | Same write-clock semantic as `pricempire_observations.timestamp`. |
| `rank` | INT | Pricempire popularity rank (lower = more popular). Wire shape: numeric string on `/prices`, native int on `/metas`. Defensive parser handles both. |
| `liquidity` | NUMERIC(6,2) | 0-100 score, quantized to 2 decimals (load-bearing for dedup tuple equality, ADR 020 §5). |
| `marketcap` | BIGINT | Up to ~10⁹ at the high end. |
| `count` | INT | Item-level listings count (not the same as the per-provider count in `pricempire_observations`). |
| `trades_7d`, `trades_30d`, `trades_90d` | INT, nullable | |
| `steam_last_24h` | INT, nullable | **Always NULL on Phase 2a-written rows.** Only present on Pricempire's `/metas` endpoint; the Phase 2a collector reads `/prices`. Reserved for a hypothetical future metas-cron. |
| `steam_last_7d`, `steam_last_30d`, `steam_last_90d` | INT, nullable | |

Dedup gate compares the full 11-field tuple against the most-recent row for the item; NULL is treated as "no change" rather than "changed to null." Steady-state row volume projected at 1-5/item/day after the first cycle (which writes 48, one per item, since the table starts empty).

### 3.3 Sources currently configured

Ten rows after migration 0007:

```
 id | name                    | enabled | denomination   | interval | per-item-delay
----+-------------------------+---------+----------------+----------+----------------
  1 | steam_market            | t       | wallet_credit  |   60     |   5
  2 | skinport                | t       | usd            |   15     |   0
  N | dmarket                 | t       | usd            |   15     |   3
  M | pricempire              | t       | NULL           |   15     |   0   (pseudo)
  M+1 | pricempire_buff163    | t       | usd            |    0     |   0   (sub-provider)
  M+2 | pricempire_buff163_buy| t       | usd            |    0     |   0
  M+3 | pricempire_skinport   | t       | usd            |    0     |   0
  M+4 | pricempire_dmarket    | t       | usd            |    0     |   0
  M+5 | pricempire_csmoney    | t       | usd            |    0     |   0
  M+6 | pricempire_swap_gg    | t       | usd            |    0     |   0
```

The `pricempire` pseudo-source row carries the schedule; the six sub-provider rows carry the FK targets for `pricempire_observations.source_id`. The scheduler explicitly skips `pricempire_*` rows when iterating enabled sources (`collectors/scheduler.py:548-549`); one Pricempire HTTP call services all six. `interval_minutes=0` on those rows is a sentinel for "not independently scheduled." ADR 018 §3.

---

## 4. Collectors

Two families with no shared business logic:

- **Per-item / per-source collectors** (`collectors/{steam,skinport,dmarket}.py`) extend `collectors.base.Collector`, yielding one `PriceObservation | DECLINED | None` per item per cycle. Persisted via `persist_observation` + `should_write_observation` (the dedup gate) into `prices`; `observation_log` is upserted *before* the dedup gate so it advances on every successful poll. (`collectors/base.py:252-293, 296-349, 352-391`; `collectors/scheduler.py:264-411`.)
- **Pricempire bulk-snapshot collector** (`collectors/pricempire.py`) lives outside the `Collector` abstraction by design (ADR 019 §1). One public entry point: `collect_snapshot()`. Streams the ~64 MB bulk response via `ijson` over `BytesIO`, writes per-provider rows into `pricempire_observations` and per-item rows into `pricempire_item_metadata`, commits per item.

The scheduler (`collectors/scheduler.py`) is DB-driven (ADR 013): on boot it reads `SELECT … FROM sources WHERE enabled = TRUE` and registers one APScheduler `interval` job per row, except `pricempire_*` sub-provider rows (skipped) and `pricempire` (registered as a pseudo-source job through `_PSEUDO_SOURCES`). Job defaults are `max_instances=1, coalesce=True, misfire_grace_time=300`.

### 4.1 Steam Community Market

- File: `collectors/steam.py`. Source name `steam_market`. Denomination `wallet_credit`.
- Endpoint: `GET https://steamcommunity.com/market/priceoverview/` (`country=US, currency=1, appid=730, market_hash_name=…`). Anonymous; no API key. Chrome-flavored UA at `collectors/base.py:53-57` (the default Python UA gets blocked immediately).
- Cadence: 60 min cycle, 5s between items (current DB values).
- Writes: `prices` row per item that returns `success:true` with a parseable `lowest_price`/`median_price`; `observation_log` row for the pair.
- **Outlier filter (ADR 006 §6, `steam.py:144-159`):** rejects observations below 20% of the item's 7-day Steam median (min 5 prior obs). Cleans the recurring `$1.00, volume=1` manipulation listings without poisoning the time series. Rejected → `DECLINED`.
- Failure handling: 4xx non-429 → `DECLINED`. 429 → AWS-style full-jitter exponential backoff, max 5 attempts, in-call sleep capped at 60s; longer waits propagate via `RateLimited` and pause the source's job via `compute_pause_seconds` / `_apply_pause` (`scheduler.py:184-260`). 5xx / timeouts → same backoff, exhaustion returns `DECLINED`.
- Quirks: `success:false` is genuine "no listings," not an error. Rare items (Howl, Dragon Lore, ★ Sport Gloves | Pandora's Box, etc.) flicker into and out of this state — the unavailability-streak insight counts the runs.

### 4.2 Skinport

- File: `collectors/skinport.py`. Source name `skinport`. Denomination `usd`.
- Endpoint: `GET https://api.skinport.com/v1/items?app_id=730&currency=USD`. Anonymous. **Brotli required** — Skinport returns 406 if `Accept-Encoding: br` is missing (`skinport.py:98-108`); `brotli` is a runtime dep so httpx decompresses transparently.
- Cadence: 15 min cycle, 1 HTTP call returning the full ~6000-item CS2 catalog; per-item delay is 0 (bulk fetch).
- Writes: `prices` + `observation_log` for each watchlist item present in the bulk response with a non-null `min_price`. `min_price` → `prices.price`; `quantity` → `prices.volume` (listings count, NOT 24h sales — ADR 008 carries the caveat for the volume-anomaly job).
- Cycle-time-stamped: every Skinport row in one cycle shares a single timestamp (the response is a server snapshot).
- Dedup: catches ~30-45 items per cycle when prices haven't moved (`prices.volume` and `prices.price` both stable). Cycle log: `Skinport cycle complete: 48 attempted, 3 written, 44 unchanged, 1 unavailable` is typical.
- Quirks: `min_price=null` is treated as ambiguous (None), not declined. Bulk fetch retry exhaustion on non-429 → all watchlist items in the cycle yield `DECLINED`.

### 4.3 DMarket

- File: `collectors/dmarket.py`. Source name `dmarket`. Denomination `usd`.
- Endpoint: `GET https://api.dmarket.com/exchange/v1/market/items?gameId=a8db&title=<name>&currency=USD&limit=100&orderBy=price&orderDir=asc`. Anonymous.
- Cadence: 15 min cycle, 3s between items.
- Writes: `prices` + `observation_log` per item. `price` = `objects[0].price.USD / 100` (DMarket sends stringified integer cents; ADR 012 §3 — beware of `suggestedPrice`, which is DMarket's recommendation, not the listing price). `volume` = `len(objects)` (stock-style listings count).
- **Title-mismatch guard (ADR 012 §4, `dmarket.py:266-295`):** DMarket's `title=` is a loose substring/prefix match. Requesting `Desert Eagle | Blaze (FN)` returns `Desert Eagle | Oxide Blaze (FN)`; requesting `M4A1-S | Cyrex (FT)` returns the StatTrak™ variant; requesting `MP9 | Hot Rod (FN)` returns the Souvenir variant. The collector enforces NFC-normalized exact-title equality on the returned `cheapest.title` and skips with a WARNING when they differ. Eight watchlist items consistently fall through this guard (`Desert Eagle | Blaze`, `M4A1-S | Cyrex (FT)`, `MP9 | Hot Rod (FN)`, `SSG 08 | Death Strike (FN)`, `Souvenir AWP | Dragon Lore (BS)`, and the three knife Fades — ★ Butterfly / ★ Huntsman / ★ Karambit — per `docs/phase2b-watchlist-proposal.md` §"Tier 5"). The DMarket alias-map fix is settled scope for Phase 2b; ADR 020 / Phase 2b explicitly do not propose replacing the direct DMarket source with Pricempire's view.
- Failure handling: 4xx non-429 → `DECLINED`. 429 / 5xx / timeout → full-jitter backoff, max 5 attempts.

### 4.4 Pricempire (bulk-snapshot, Phase 2a)

- File: `collectors/pricempire.py`. Pseudo-source name `pricempire`; six sub-providers `pricempire_buff163`, `pricempire_buff163_buy`, `pricempire_skinport`, `pricempire_dmarket`, `pricempire_csmoney`, `pricempire_swap_gg`.
- Endpoint: `GET https://api.pricempire.com/v4/paid/items/prices?app_id=730&sources=buff163,buff163_buy,csmoney,dmarket,skinport,swapgg`. Bearer auth via `PRICEMPIRE_API_KEY`. Missing key → fail-fast with ERROR log, no HTTP call.
- Cadence: 15 min cycle. One HTTP call returns the entire CS2 catalog (~39,400 items, ~64 MB at the six-source filter). ~4-7s wall time per cycle.
- Memory: response body held in memory as bytes; `ijson.items(BytesIO(response.content), "item", use_float=True)` streams item dicts so the Python-object peak stays in the low MB range. `use_float=True` is load-bearing — ijson's default `Decimal` decoding breaks JSON-serialization into `raw_response` JSONB mid-stream (ADR 019 §2; first live cycle exhibited this bug after 14 items).
- Writes: For each item in our `items` table, persists one row per `pricempire_observations` per provider (price + count) AND one row to `pricempire_item_metadata` per item. Both writes pass through their own dedup gates. Per-item commit cadence. Wire price is integer cents; parsed via `Decimal(str(raw_price)) / 100` (defensive across int/float wire formats — ADR 019 §3).
- Phase 2a scope: only writes rows for items already in the watchlist. The ~39,344 catalog items not in the watchlist are counted as `items_skipped_unknown` and logged at cycle end. Phase 2b adds the long-tail layer.
- Failure handling: any `httpx.HTTPStatusError` / `httpx.RequestError` → WARNING log, exit cleanly, next 15-minute tick is the retry. No in-call retries (ADR 019 §6).
- Cycle-complete log line example (`pricempire.py:309-324`): `Pricempire cycle complete: 39392 items seen, 39344 skipped (not in watchlist), 35 rows written (buff163=8, buff163_buy=6, csmoney=10, dmarket=4, skinport=7, swap_gg=0), 247 unchanged (...), 0 skipped (unknown provider); metadata: 0 written, 48 unchanged; elapsed 5.3s`.
- **Quirks captured during Phase 2a ingest validation** (`docs/phase2a-ingest-validation.md`):
  - **No `observation_log` analog yet** for Pricempire — Phase 2b decides whether one is needed. Until then, the dedup gate compares against `pricempire_observations` itself.
  - **Doppler / phase-bearing taxonomy mismatch.** Direct Skinport vs `pricempire_skinport` drift exceeds 60% for the three Doppler-pattern items in the current watchlist (Karambit / Flip Knife / M9 Bayonet Doppler FN). Not a bug — Skinport's `market_hash_name` groups all Doppler phases under one name; the cheapest listing on our direct collector may be a high-phase outlier while Pricempire normalizes differently. Pattern-sensitivity extends beyond Dopplers to Marble Fade, Tiger Tooth, and Sport Gloves Vice (which has pattern-seed rarity). Phase 2b drift detection will need either per-phase splits or an explicit pattern-aware skip rule.
  - **swap.gg correctly quiet.** Over a 13.5-hour validation window the `pricempire_swap_gg` source produced one non-initial write (44 first-cycle rows + USP-S | Kill Confirmed moving once). Probed against Pricempire's current state: every covered item's DB value matched Pricempire's exactly. The dedup gate is correctly suppressing a low-liquidity sub-marketplace whose listed prices barely move within a 15-min window; the collector is not dropping data.
  - **`updated_at` placeholders.** ~6% of `pricempire_skinport` rows and ~29% of `pricempire_swap_gg` rows carry `updated_at = 2025-01-01T00:00:00Z`. `last_checked_at` is always real-time. Phase 2b drift logic should drive off `last_checked_at` exclusively.
  - **Pricempire does not serve Steam Market prices.** Passing `sources=steam` returns 200 with zero Steam rows. The Steam collector remains the only Steam-pricing source. (ADR 018 §"Context".)

---

## 5. API surface

FastAPI app at `api.main:app`, mounted on host port `127.0.0.1:8001` (container port 8000). Every router carries a `Depends(require_token)` checking `Authorization: Bearer <SKIN_MARKET_API_TOKEN>` (ADR 014 §10, `api/main.py:60-66`). `/health` is the lone unauthenticated exception — Docker's healthcheck calls it from inside the container without credentials.

Money is serialized as a string in JSON (`MoneyStr = Annotated[Decimal, PlainSerializer(str, …)]`). Every per-source price field is paired with the source's `denomination` — there is deliberately no top-level scalar `price`, so the bot can't accidentally collapse `usd` and `wallet_credit` (`api/schemas.py:9-17`).

### Endpoints used by the bot

| Route | Source of truth | Freshness contract |
|---|---|---|
| `GET /health` | `SELECT 1` against Postgres | Returns `{status: "ok", db: "reachable" \| "unreachable"}`. Unauthenticated. |
| `GET /items` | `items` ordered by `display_name` | Watchlist registry. No pagination. |
| `GET /items/{slug}` | `items WHERE slug = :slug` | 404 on miss. |
| `GET /items/{slug}/price` | `observation_log` LEFT-JOIN-LATERAL `prices` | **Phase 1 / ADR 017 split.** Each per-source row carries `last_polled_at` (from `observation_log.last_observed_at`, the freshness signal) AND `last_changed_at` (from `prices.timestamp`, the last time `(price, volume)` actually moved). Sources with no `observation_log` row are omitted; the bot fills the slot with `never_observed`. |
| `GET /items/{slug}/history` | `prices` filtered by `slug`, optional `source`, `since`, `until` | Default `since` = now-7d, default `limit` = 500, hard cap 5000. Returns `prices.timestamp` only — no `observation_log` involvement (this is a price-movement series). |
| `GET /items/{slug}/insights` | `insights` (excluding `daily_narrative`), latest per `(insight_type, meta_signature)` | `meta_signature` is composed of `source_id` (per-source insights) or `source_a_id/source_b_id` (cross-source insights) so multiple sub-keys of the same insight type survive the DISTINCT ON. |
| `GET /items/{slug}/chart` | `prices` for one `(item, source)`, rendered to PNG by matplotlib | `matplotlib` imported inside the handler to keep cold-start fast. Dark "tokyo-night" theme; per-source line colors (`skinport=blue, dmarket=green, steam_market=amber`). |
| `POST /deals/evaluate` | Same `observation_log`-driven query shape as `/items/{slug}/price` | **Same Phase 1 split.** Currency mismatch → informational with `reason=denomination_mismatch`. Comparable rows whose `last_polled_at` is older than `COMPARABLE_FRESHNESS_HOURS = 4` → informational with `reason=stale`. Verdict math reads only fresh, currency-matched comparables; otherwise verdict is `no_comparable_data`. Tolerance band ±5% around `min(comparable.current)` (`api/routes/deals.py:52-59`). |
| `GET /insights/narrative/latest` | `insights WHERE insight_type='daily_narrative' ORDER BY computed_at DESC LIMIT 1` | 404 if no narrative row has been generated yet. |
| `GET /insights/anomalies/recent` | `insights` filtered to `cross_source_divergence` + `volume_anomaly`, joined with `items` for slug/display_name | Default window 6h, max 24h. Z-scores are signed. |

The API does NOT currently touch `pricempire_observations` or `pricempire_item_metadata` — those are Phase 2b consumers (drift detection, long-tail lookups). Until that wiring lands, the `enabled = TRUE` flag on the six `pricempire_*` sub-provider rows has no behavioral effect on any endpoint above (migration 0006's docstring spells this out).

---

## 6. Bot

`bot/main.py` runs a discord.py 2.7.1 client; `message_content` intent is set (must also be enabled in the Discord developer portal — README has the toggle).

**Triggering** (`bot/main.py:115-119`): DMs, or @-mentions in guild channels. No passive listening (architectural commitment, ARCHITECTURE.md "out of scope").

**Access control** (`bot/main.py:122-135`): every author is checked against `DISCORD_ALLOWED_USER_IDS`. Empty allowlist → fail-closed "config error" reply. Disallowed users get one "not authorized" reply per process lifetime, then suppressed.

**Model.** `huihui_ai/Qwen3.6-abliterated:27b` running locally on Ollama at `host.docker.internal:11434` (default; overridable via `OLLAMA_MODEL`). The analytics narrative job shares the same env var by default.

**Tool-calling pattern** (`bot/ollama_client.py`). Uses `ollama.AsyncClient.chat(model=…, messages=…, tools=TOOL_DEFINITIONS)` — i.e. the standard chat-completion endpoint with `tools=[…]` in the request payload. This is "Default" mode in Open WebUI terms, NOT Ollama's "Native" tool-calling variant. The Native path was found unreliable for Qwen3-abliterated and is documented as **load-bearing** in ADR 016 and the project's persistent memory.

The tool-use loop is capped at `MAX_TOOL_CALLS = 5` sequential rounds to prevent runaway loops on malformed tool calls. Each tool body is synchronous (`bot/tools.py`); the bot wraps each call in `asyncio.to_thread` so blocking HTTP doesn't stall the discord.py heartbeat. Defensive handling covers: JSON-string `arguments` instead of dict, unknown tool names, missing/extra kwargs, tool exceptions — all converted into `tool_result` strings the model can render around, rather than crashes.

Ollama timeout is 300s (`OLLAMA_TIMEOUT_SECONDS`). Comment at `bot/ollama_client.py:82-91` is explicit that this is a band-aid; the real fix is the size-discipline summarizers in `bot/tools.py`.

**Tools** (declared in `TOOL_DEFINITIONS` and dispatched via `TOOL_FUNCTIONS`):

| Tool | Calls | Returns | Size discipline |
|---|---|---|---|
| `list_watchlist()` | `GET /items` | `{count, by_category, sample}` summary | Heuristic CS2 category map; max sample 5. |
| `query_current_price(slug)` | `GET /items/{slug}/price` + `GET /items/{slug}/insights` | Per-source list with `state ∈ {fresh, stale, unavailable, never_observed}` + optional `price_flat_minutes` informational field + optional `anomaly_flag` summary. | Three-state composer is the post-ADR-017 logic: `state` is driven by `last_polled_at`, NOT `last_changed_at`. `price_flat_minutes` is surfaced only when `last_polled_at - last_changed_at ≥ 60min`, and the system prompt is explicit that it's NOT a warning. |
| `query_price_history(slug, source?, days=7, limit=500)` | `GET /items/{slug}/history` | Raw rows when ≤30; aggregate per-source stats (first/last/min/max/count) otherwise. | `HISTORY_DOWNSAMPLE_THRESHOLD=30`. |
| `render_chart(slug, source='skinport', days=7)` | `GET /items/{slug}/chart` | `Attachment` dataclass — PNG bytes + filename, uploaded as a Discord file by the renderer. | Single-source by design (ADR 014 §6). |
| `evaluate_deal(slug, amount, currency)` | `POST /deals/evaluate` | Verdict + summary + comparable/informational rows. | API-side freshness gate is 4h on `last_polled_at`. |
| `narrative_today()` | `GET /insights/narrative/latest` | `{computed_at, text, meta: {as_of, cited_count}}`. | Citation rows collapsed into a count; the model only needs the paragraph. |
| `whats_interesting(hours=6)` | `GET /insights/anomalies/recent` | Top-N by `|z|` if more than 10, with `total_count`; else passthrough. | `ANOMALIES_TOP_N_THRESHOLD=10`. |

**System prompt** (`bot/system_prompt.py`). One triple-quoted string. Key features:

- Explicit `# CRITICAL: never answer from memory` block telling the model that every factual claim about CS2 prices MUST come from a tool call (the training data is unreliable).
- Trigger-phrase lists per tool — open-source models need more steering than cloud APIs (ADR 016 §"Defensive handling").
- Hard-coded slug normalization rules (`★` → `star-`, `™` → empty, `Souvenir` prefix preserved).
- Denomination-discipline rule: never collapse `usd` and `wallet_credit`; first mention of a wallet-credit price in a reply must include the "SC" footnote.
- Explicit instruction that `🟡 stale` is driven by `last_polled_at` and that `price_flat_minutes` must NOT be rendered with 🟡 (per ADR 017).

The bot does NOT read Postgres directly. Every number it cites comes through the read API.

---

## 7. ADR index

All under `docs/adr/`.

| # | Title | Why it matters |
|---|---|---|
| 001 | Use `uv` for Python dependency management | Locks the toolchain; image build uses `uv sync --frozen --no-dev`. |
| 002 | TimescaleDB over vanilla PostgreSQL | Justifies the hypertable choice on `prices` (and now `pricempire_observations`, `pricempire_item_metadata`). |
| 003 | FastAPI over Flask or Django for the read API | |
| 004 | psycopg 3 over psycopg2-binary | |
| 005 | Auto-generated slugs with a fixed glyph map | The slug rules in the bot's system prompt mirror this. |
| 006 | Collector resilience strategy | Full-jitter backoff, the DECLINED vs ambiguous-None split, the Steam outlier filter. |
| 007 | `insights.text_value TEXT` column for narrative insights | Why the daily narrative isn't squeezed into JSONB. |
| 008 | Skinport collector: bulk fetch, filter in Python, mapping | Documents `quantity` = listings count, NOT 24h sales. |
| 009 | Scheduler design | Dedup-on-write semantics on `prices` — exact `(price, volume)` equality, no tolerance. |
| 010 | Analytics design: source-dynamic, divergence-first, SQL-native | Why the anomaly bar is *divergence* and not absolute magnitude. |
| 011 | Narrative job: LLM choice, prompt structure, deployment | |
| 012 | DMarket collector: second real-money source | The `title=` substring trap and the `suggestedPrice` vs `price.USD` trap. |
| 013 | Rate-limit policy | The DB-driven `interval_minutes` / `per_item_delay_seconds` columns + the source-pause ladder. |
| 014 | Read API design | `MoneyStr`, denomination pairing, single-bearer token, deals tolerance, single-source charts. |
| 015 | Hermes bot skill design | Pre-Phase-7c plan; superseded by the in-process Ollama-tools approach in ADR 016. Carries the unavailability-streak rationale. |
| 016 | Discord bot runtime (Phase 7c) | Default vs Native tool-calling on Ollama (load-bearing); tool-result size discipline; defensive handling of malformed tool calls. |
| 017 | Split `observed_at` into `last_polled_at` and `last_changed_at` | Phase 1 resolution of the dedup-vs-display freshness bug — drives the bot's 🟡 stale rendering off `observation_log`, not `prices`. |
| 018 | Pricempire as breadth-coverage data source | Separate hypertable; six sub-providers + one pseudo-source; three timestamps on each observation; Phase 2a watchlist-only scope. |
| 019 | Pricempire collector design | Why it doesn't extend `BaseCollector`; `ijson` streaming with `use_float=True`; defensive cents-parse; per-item commit. |
| 020 | Pricempire item-metadata extraction | Side-effect-of-price-ingest pattern; separate `pricempire_item_metadata` hypertable; defensive int-coercer for Pricempire's inconsistent wire types; `steam_last_24h` reserved column. |

---

## 8. Known gotchas

The load-bearing ones, in rough order of how often they will trip up a new contributor or future operator:

1. **Dedup-driven freshness blindness on `pricempire_observations.timestamp`.** Phase 2a's dedup gate suppresses writes when `(price, count)` matches the latest row for the pair. That means `MAX(pricempire_observations.timestamp)` for a `(item, source)` pair is "when the price last changed in Pricempire's view," NOT "when Pricempire last polled the provider." Phase 2a's 13.5-hour validation window shows max-age values of 815 min on at least one pair per provider — that does NOT mean Pricempire stopped refreshing. The honest poll-freshness signal is `raw_response->>'last_checked_at'`. Any Phase 2b drift / freshness logic that drives off `pricempire_observations.timestamp` will re-create Phase 1's `observed_at` bug.

2. **Doppler / phase-bearing items show >60% drift between direct Skinport and `pricempire_skinport`.** Skinport's `market_hash_name` groups all Doppler phases under one name; our direct collector pulls one listing's price (often a high-phase outlier) while Pricempire normalizes differently. This is upstream taxonomy aggregation, not a bug. Affects ★ Karambit / ★ Flip Knife / ★ M9 Bayonet Doppler FN today.

3. **Pattern-sensitivity extends beyond Dopplers.** ★ Karambit Marble Fade FN, ★ Karambit Tiger Tooth FN, and ★ Sport Gloves Vice FT all carry phase- or pattern-seed-dependent prices that the upstream `market_hash_name` doesn't distinguish. Phase 2b drift logic must classify these explicitly or skip them — see Phase 2b proposal, Tier 3 "Pattern-sensitivity risk" column.

4. **No `observation_log` analog for `pricempire_observations` yet.** Phase 2a's dedup gate compares against `pricempire_observations` itself, and no "we polled this pair at time T" table exists for Pricempire. Phase 2b decides whether to add one; until then, the only honest Pricempire-side poll-freshness signal is the per-row `last_checked_at`. The unavailability-streak analytics (`analytics/unavailability_streak.py`) operates only on the curated collectors via `observation_log`.

5. **DMarket title-mismatch eight-item gap.** Eight watchlist items consistently fall through the title-equality guard each DMarket cycle: `Desert Eagle | Blaze (FN)`, `M4A1-S | Cyrex (FT)`, `MP9 | Hot Rod (FN)`, `SSG 08 | Death Strike (FN)`, `Souvenir AWP | Dragon Lore (BS)`, `★ Butterfly Knife | Fade (FN)`, `★ Huntsman Knife | Fade (FN)`, `★ Karambit | Fade (FN)`. Pricempire's all-six-providers probe confirms each one IS available upstream on DMarket — the fix scope is settled: repair the direct collector's title-matcher (alias map in `data/watchlist.yaml`), do NOT replace the direct DMarket source with Pricempire's view. ADR 020 / Phase 2b proposal §"Phase 2b directions (settled, not open)."

6. **Skinport's `quantity` is listings count, not 24h sales.** The unified `prices.volume` column means the volume-anomaly insight is meaningful only for Steam (where volume is 24h sales). The current SQL filters by `s.denomination = 'wallet_credit'` as a proxy for "is this a flow-style source" — a TODO sits at the top of `analytics/anomaly_detection.py` noting this needs a direct flag once a second flow-style source lands.

7. **`steam_last_24h` is always NULL.** The Phase 2a Pricempire collector reads `/v4/paid/items/prices`, which doesn't carry that field. The column exists on `pricempire_item_metadata` for forward compatibility with a hypothetical metas-cron; do not interpret its NULL-ness as "no Steam 24h data available," it's "we haven't built the metas path yet."

8. **`prices` compression policy is enabled (30-day threshold); `pricempire_observations` is uncompressed.** Migration 0001 sets `add_compression_policy('prices', INTERVAL '30 days')` with `compress_segmentby = 'item_id, source_id'`. Migration 0005 deliberately does NOT add one for `pricempire_observations` — deferred until storage warrants tuning. Same story for `pricempire_item_metadata` (migration 0007). No retention policies anywhere.

9. **`pricempire_*` sub-provider rows are `enabled=TRUE` but not independently scheduled.** Migration 0006 flips them on; the scheduler explicitly skips them (`collectors/scheduler.py:548-549`). The flag means "yes, the system is actively ingesting data for this sub-provider" — not "schedule a per-source job for it." Downstream queries that filter `WHERE s.enabled = TRUE` and reach `pricempire_observations` see these as live sources without special-casing.

10. **ARCHITECTURE.md's architecture ASCII diagram still references a "Hermes Discord bot" host process.** The bot lives in compose now (Phase 7c, ADR 016). Cosmetic but worth knowing if you read the diagram first and the code second.

---

## 9. Phase status

| Phase | State | Next |
|---|---|---|
| Phase 1 — `observation_log` + observed-at timestamp split | **Done.** ADR 017 lands the split across `/items/{slug}/price` and `/deals/evaluate`; bot reads `last_polled_at` for the freshness decision and surfaces `last_changed_at` informationally as `price_flat_minutes`. Regression tests pin the behavior. | — |
| Phase 2a — Pricempire breadth ingest | **Done.** Migrations 0005-0007 deployed; collector writing `pricempire_observations` + `pricempire_item_metadata` every 15 min. Phase 2a ingest validation completed against a 13.5h window (`docs/phase2a-ingest-validation.md`). Pricempire data does NOT flow into any API endpoint, analytics job, or bot tool yet — by design (ADR 018 §"Consequences"). | Hand off ingest validation findings to Phase 2b. |
| Phase 2b — Watchlist re-seed + drift detection | **Proposed.** `docs/phase2b-watchlist-proposal.md` is a draft: 41-item composition across Tier 1/2/3/5; Doppler items entirely excluded; DMarket title-mismatch fix scoped to an alias map. Open questions still in the doc; awaits human review. | Accept/edit/reject each tier; execute the re-seed; build drift detection against `pricempire_observations.last_checked_at`. |
| Out of scope for v1 (unchanged) | CSFloat float-tier (v2), news/speculation layer (v3), multi-game (v4), web frontend (v5), accounts/auth (v5+), payments (v5+), real-time websockets (v6+). | — |
| Operational gaps (unchanged) | No metrics endpoint / Prometheus / Grafana / alerting. No log shipping. No per-route latency or Ollama token-usage instrumentation. No retention policies. | All deferred. |

---

## 10. Workflow rules

- **Staging discipline: schema → logic → UI.** Phase 2a deliberately split into three commits — schema (migration 0005), collector logic + scheduler wire-up, then the metadata follow-up (migration 0007 + extraction). Same for Phase 1: ADR 017 / `/items/{slug}/price` first; `/deals/evaluate` left for a follow-up rather than bundled. Pattern: ship the schema with the table empty and `enabled=FALSE`, ship the logic that fills it, then flip the flag — keeps each diff reviewable and rollbacks cheap.
- **ADRs are first-class artifacts.** Any non-obvious architectural choice — library selection, schema shape, scheduling pattern, freshness contract — lands as `docs/adr/NNN-title.md`. Phase 2a alone produced ADRs 018, 019, 020. Pre-existing pattern: 17 ADRs as of the audit.
- **Pushback on scope-additive requests is expected.** ARCHITECTURE.md and the Phase 2b proposal both call this out explicitly. The brief for the watchlist proposal asked for "settled vs open" framing — three of the four "Phase 2b directions" rows are explicitly settled, with only one open question per tier escalated for human review.
- **The mantra.** *Before debugging a symptom, state in one sentence what the app is supposed to do in that state. If you can't, that's the first thing to figure out.* (ARCHITECTURE.md.) Phase 1's `observed_at` bug is the canonical lesson: the symptom was "Skinport items render 🟡 stale every cycle." The one-sentence statement of intent was "🟡 should mean we haven't polled in 4 hours." Once both were on the table, the bug was a query change, not a schedule change.
- **Boring libraries.** No exotic frameworks. The full tech surface a future operator needs to debug at 2am is httpx / sqlalchemy / fastapi / apscheduler / matplotlib / pandas / discord.py / ollama-python, plus ijson for the one bulk-streaming case.
- **No money floats.** `NUMERIC(12,2)` in Postgres, `Decimal` in Python, string-typed `MoneyStr` on the API wire. The whole stack enforces this; any `float(price)` is a bug.
- **Tests for non-trivial logic.** Pure data-shuffling untested; pricing math, dedup, schema migrations, the title-mismatch guard, the Doppler drift, the `use_float=True` regression all carry tests in `tests/`.

---

## Audit notes — what changed vs. the stale file

- **§0's "There is no Pricempire integration" framing is gone.** That was correct at the time but is now load-bearingly wrong: Pricempire lands as a fourth collector family in Phase 2a (ADR 018/019/020, migrations 0005-0007). The new file treats it as a first-class layer.
- **Sources table grew from 3 rows to 10.** Migration 0005 adds six `pricempire_*` sub-provider rows + one `pricempire` pseudo-source; migration 0006 enables them.
- **Two new hypertables.** `pricempire_observations` (composite PK with three distinct timestamps — write clock, Pricempire's `last_checked_at`, Pricempire's `updated_at`) and `pricempire_item_metadata` (per-item slow-changing fields lifted out of `raw_response` JSONB into typed columns). Neither has a compression or retention policy yet.
- **ADR count grew 16 → 20.** 017 (observed-at split — Phase 1 close-out), 018 / 019 (Pricempire breadth + collector design), 020 (item-metadata extraction).
- **§5's "4-hour staleness issue" historical write-up is dropped.** Phase 1 closed it; the design is now part of §3.1 (`observation_log` row), §5 (Phase 1 split surfaced in `PerSourcePrice`), and §6 (bot's three-state composer + `price_flat_minutes`). The new file mentions the fix's consequence — that source rows without an `observation_log` entry are honestly omitted by the API — but doesn't reproduce the audit narrative.
- **`prices` compression policy correction.** The stale file said "no compression policy on the `prices` hypertable." Migration 0001 actually does add one (30-day threshold, `compress_segmentby = 'item_id, source_id'`). The stale file's "what's missing" bullet on this was simply wrong. (Whether the compression policy *fires* in practice depends on chunk age; with v1 going live recently, the first chunk only crosses the threshold a month after deploy. The policy is there.)
- **DMarket id correction (cosmetic).** The stale file noted "dmarket `id=27` is a tell that the sources row was rewritten." That fact is still true if the DB ever gets re-bootstrapped, but the new file doesn't surface specific row IDs — they're not load-bearing and would drift on any redeploy.
- **Unverified statements the stale file carried that this audit could not confirm.** The stale file's `docker ps` snapshot ("Up 44 hours" etc.) couldn't be re-verified from the code alone — it was a live-system observation. Anything operational like that has been dropped from this version; the new file describes the *intended* runtime state from the compose file, not the in-the-moment state.
- **Surprises during this audit.**
  - The `pricempire` pseudo-source pattern is cleaner than expected — `_PSEUDO_SOURCES` is one line of code in the scheduler, and the same shape would absorb any future bulk-snapshot source (e.g. a Pricempire `/metas` cron) without further plumbing.
  - The Pricempire collector's `_persist_metadata` dedup tuple includes `liquidity` quantized to two decimals. Without that quantize, the float wire value (`62.802508437142585`) would never tuple-equal the stored `Decimal('62.80')` and the gate would write every cycle. Small detail; load-bearing for the projected "1-5 rows/item/day" steady state.
  - The validation doc's swap.gg characterization is more rigorous than expected — it cross-checks the live DB against a cached Pricempire probe to prove the dedup gate is correct rather than just asserting it. That diligence raised the §8 gotcha #1 to load-bearing status for Phase 2b.
