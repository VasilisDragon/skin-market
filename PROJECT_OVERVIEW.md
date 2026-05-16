# skin-market — Project Overview (read-only audit)

> Snapshot taken 2026-05-15 ~20:45 UTC against the running `docker compose` stack on this host. Container uptime ~44h for `collector`/`bot`/`api`/`postgres`, ~2d for `analytics`.

---

## 0. Important up-front correction: there is no Pricempire integration

The audit brief asks about a **Pricempire integration**. There isn't one. The only occurrence of "Pricempire" anywhere in the repo is a one-line mention in `ARCHITECTURE.md` line 5 where it is listed as a *competitor* the project aims to displace, not a data source.

The actual upstream price sources are **Steam Community Market, Skinport, and DMarket** — each polled directly by its own collector. If the operator is seeing the bot answer with what looks like Pricempire data, they are misreading the source attribution; nothing in this codebase calls api.pricempire.com or similar.

Everything in §3 below answers the brief's "Pricempire integration" questions against those three actual collectors.

---

## 1. Stack and runtime

**Languages / runtime**

- Python `>=3.11,<3.13` per `pyproject.toml:6`. Container base image is `python:3.12-slim` (`Dockerfile:6`).
- Dependency manager: `uv` (Astral) — `uv sync --frozen --no-dev` at image build time, ADR 001.

**Key library versions (resolved in `uv.lock`)**

| Library | Version | Used for |
|---|---|---|
| `fastapi` | 0.136.1 | read-only API (`api/main.py`) |
| `uvicorn[standard]` | 0.46.0 | ASGI server, single process per `api/` container |
| `sqlalchemy` | 2.0.49 | ORM (`db/models.py`) |
| `psycopg[binary]` | 3.3.4 | Postgres driver (ADR 004 — psycopg3, not psycopg2) |
| `alembic` | 1.18.4 | Migrations under `db/migrations/versions/` |
| `apscheduler` | 3.11.2 | Per-source poll scheduling (collectors + analytics) |
| `httpx` | 0.28.1 | All outbound HTTP |
| `brotli` | 1.x | Required by Skinport's `Accept-Encoding: br` (else 406) |
| `pydantic` | 2.13.4 | API schemas (`api/schemas.py`) |
| `matplotlib` | 3.10.9 | Chart PNG rendering (`api/routes/charts.py`) |
| `pandas` | 2.2.x | Used by analytics |
| `discord.py` | 2.7.1 | Discord bot client |
| `ollama` | 0.6.2 | Local LLM tool-calling client |
| `ruamel.yaml` | 0.18.x | Watchlist edits that preserve comments |
| `pytest` | 8.3.x | Tests under `tests/` |

**Process model**

Five containers, all built from the same `Dockerfile` (only `command:` differs). Defined in `docker-compose.yml`:

| Service | Container | Command | Purpose |
|---|---|---|---|
| `postgres` | `skinmarket-postgres` | `timescale/timescaledb:2.17.2-pg16` | Sole stateful service. Bound to `127.0.0.1:5432`. |
| `collector` | `skinmarket-collector` | `python -m collectors.scheduler` | One `BlockingScheduler`, one APScheduler job per enabled source. `stop_grace_period: 5m`. |
| `analytics` | `skinmarket-analytics` | `python -m analytics.scheduler` | One `BlockingScheduler`, hourly + daily-02:00-UTC jobs. |
| `api` | `skinmarket-api` | `uvicorn api.main:app --host 0.0.0.0 --port 8000 --proxy-headers` | Bound to host `127.0.0.1:8001 → 8000`. Read-only. |
| `bot` | `skinmarket-bot` | `python -m bot.main` | Single-process discord.py event loop (`bot/main.py:177`). No exposed port (outbound-only). |

There are no systemd units or a process manager beyond Docker `restart: unless-stopped`. Logging is JSON-lines to stdout per service with the daemon-level rotation `max-size=50m, max-file=5` declared via the `x-logging` YAML anchor at the top of `docker-compose.yml`.

The bot is a single-process asyncio event loop — no worker pool, no sharding. Tool function bodies are sync I/O and are wrapped per call in `asyncio.to_thread` (`bot/ollama_client.py:174`) so blocking HTTP to the API doesn't stall the discord.py heartbeat.

**Deployment location**

Running on this host (referred to in `ARCHITECTURE.md` as the "DGX Spark"). `docker ps` confirms all five containers up:

```
skinmarket-bot         Up 44 hours
skinmarket-api         Up 44 hours (healthy)   127.0.0.1:8001->8000/tcp
skinmarket-collector   Up 44 hours
skinmarket-postgres    Up 44 hours (healthy)   127.0.0.1:5432->5432/tcp
skinmarket-analytics   Up 2 days
```

`Ollama` itself is NOT a compose service — it lives on the host and is reached via `host.docker.internal:11434` (`docker-compose.yml:129,167`, `extra_hosts: host.docker.internal:host-gateway`).

---

## 2. Data layer

**Engine** — Postgres 16.6 (Alpine, aarch64) with the **TimescaleDB 2.17.2** extension enabled. Image `timescale/timescaledb:2.17.2-pg16` (`docker-compose.yml:23`). Database name `skinmarket`, user `skinmarket`. DB size at audit time: **30 MB**.

**Connection config** — Two URLs in `.env`:

- Host-side `DATABASE_URL=postgresql+psycopg://skinmarket:<password>@localhost:5432/skinmarket` — used by Alembic and ad-hoc scripts. The actual password is checked in (`.env` is git-ignored but exists locally with a real 32-char value).
- Compose-internal: each service builds its own URL at boot from `POSTGRES_*` against the `postgres` service hostname (e.g. `docker-compose.yml:52`).

Postgres password rotation is a known footgun, called out in the compose file header — `POSTGRES_PASSWORD` is only read on first init of the data volume; later changes require `ALTER USER` inside the running container.

**Migrations** — Alembic head is `0004` (`SELECT version_num FROM alembic_version`). Migrations checked into `db/migrations/versions/`:

1. `0001_initial_schema.py`
2. `0002_phase5_schema_additions.py`
3. `0003_rate_limit_policy.py` — added `interval_minutes`, `per_item_delay_seconds` to `sources`
4. `0004_observation_log.py` — added `observation_log` table

**Tables (all in `public`)** — 6 total, 5 application tables + `alembic_version`:

| Table | Cols (PK / important) | Rows now | Indexes / notes |
|---|---|---|---|
| `items` | `id UUID PK (gen_random_uuid)`, `market_hash_name TEXT UNIQUE`, `display_name`, `slug TEXT UNIQUE`, `item_type`, `weapon_name`, `skin_name`, `wear`, `is_stattrak BOOL`, `is_souvenir BOOL`, `created_at` | **48** | `items_pkey`, `items_market_hash_name_key`, `items_slug_key` |
| `sources` | `id SERIAL PK`, `name TEXT UNIQUE`, `base_url`, `rate_limit_per_minute`, `enabled BOOL`, `denomination TEXT`, `interval_minutes`, `per_item_delay_seconds` | **3** | `sources_name_key` |
| `prices` | composite PK `(item_id, source_id, timestamp)`; `price NUMERIC(12,2)`, `volume INT`, `currency VARCHAR(8) DEFAULT 'USD'`, `raw_response JSONB` | **5,564** | Indexes: `prices_pkey`, `prices_timestamp_idx (timestamp DESC)`. **TimescaleDB hypertable, 8 chunks.** |
| `observation_log` | composite PK `(item_id, source_id)`, `last_observed_at TIMESTAMPTZ` | **126** | Phase 7a addition (migration 0004). Upserted unconditionally on every successful poll; **decoupled from dedup-on-write** of `prices`. |
| `insights` | `id BIGSERIAL`, `item_id`, `computed_at`, `insight_type TEXT`, `value NUMERIC`, `text_value TEXT`, `meta_info JSONB` | **34,270** | `ix_insights_item_type_computed_desc(item_id, insight_type, computed_at DESC)` |
| `alembic_version` | `version_num` | 1 row (`0004`) | — |

**Sources currently configured**:

| id | name | enabled | denomination | interval_minutes | per_item_delay_seconds | rate_limit_per_minute |
|---|---|---|---|---|---|---|
| 1 | steam_market | t | wallet_credit | 60 | 5 | 12 |
| 2 | skinport | t | usd | 15 | 0 | 60 |
| 27 | dmarket | t | usd | 15 | 3 | 20 |

The dmarket `id=27` is a tell that the sources row was rewritten at some point (not a problem; just background context).

**Insight type distribution** (`SELECT insight_type, COUNT(*) FROM insights GROUP BY 1`):

```
moving_avg_30d              11032
moving_avg_7d               11032
cross_source_spread          5648
cross_source_view            3973
item_unavailability_streak   1535
cross_source_divergence       845
volume_anomaly                201
daily_narrative                 4
```

Only 4 `daily_narrative` rows exist — the 02:00 UTC narrative job has only succeeded on 2026-05-12 through 2026-05-15.

**Historical vs latest** — Prices are stored **historically**, but with a `(price, volume) != latest` dedup gate (`collectors/base.py:352` `should_write_observation`). An unchanged poll writes nothing to `prices`; only `observation_log` is upserted. This dedup is the root of the staleness issue documented in §5.

**Retention policy** — None. There is no `drop_chunks` policy, no `add_retention_policy(...)`, no cron job pruning old `prices` or `insights`. Both tables grow unboundedly. At ~5.5k prices rows in 4 days the trajectory is ~40k/month; at ~34k insights rows in 4 days the trajectory is ~250k/month. Both modest, but worth recording as "no plan exists."

---

## 3. Upstream price-source integration (Steam / Skinport / DMarket — see §0)

**Files & endpoints**

| Source | File | Endpoint | Pattern |
|---|---|---|---|
| Steam | `collectors/steam.py:77` | `GET https://steamcommunity.com/market/priceoverview/` with `country=US, currency=1, appid=730, market_hash_name=…` | **Per-item** — one GET per item |
| Skinport | `collectors/skinport.py:64` | `GET https://api.skinport.com/v1/items?app_id=730&currency=USD` | **Bulk** — one GET returns the full ~6000-item catalog; we filter to our 48 in Python |
| DMarket | `collectors/dmarket.py:80` | `GET https://api.dmarket.com/exchange/v1/market/items?gameId=a8db&title=<name>&currency=USD&limit=100&orderBy=price&orderDir=asc` | **Per-item** — one GET per item |

**Scheduling** — DB-driven via `collectors/scheduler.py`. At startup, `_load_enabled_sources` (`scheduler.py:147`) reads `SELECT … FROM sources WHERE enabled=TRUE`, then `build_scheduler` (`scheduler.py:476`) registers one APScheduler `interval` job per enabled source. Per-source cadence with current DB values:

- `steam_market`: every **60 minutes**, 5s between items per cycle (50-item ceiling, never reached because watchlist is 48)
- `skinport`: every **15 minutes**, single bulk fetch (per-item delay = 0)
- `dmarket`: every **15 minutes**, 3s between items per cycle

Job defaults are `max_instances=1, coalesce=True, misfire_grace_time=300`. A still-running cycle whose next tick fires is skipped (logged) rather than running concurrently.

**Watchlist (the polled universe)**

- **48 items** total. Source of truth: `data/watchlist.yaml`; loader is `scripts/seed_watchlist.py` (writes both `items` and `sources` tables). Mix per the file header: ~25% rifles, ~20% snipers, ~25% knives, ~15% gloves, ~10% pistols, ~5% other.
- The DB has 48 items; YAML has 48 item entries; they match.
- Watchlist edits go through `scripts/watchlist_edit.py` (preserves comments via `ruamel.yaml`).
- All 48 items are polled in every cycle for all three sources. No slicing/rotation — the Steam 50-item ceiling exists but isn't engaged. A `TODO(watchlist-rotation)` lives at `collectors/scheduler.py:178` flagging that naive slicing would starve later items if the watchlist grows past 50.

**Batching**

- Skinport: 1 HTTP call per cycle → all 48 items resolved from the bulk response.
- Steam: 48 HTTP calls per cycle, serialized with `inter_request_delay=5s` (cycle wall time ≈ 4 minutes).
- DMarket: 48 HTTP calls per cycle, serialized with `inter_request_delay=3s` (cycle wall time ≈ 2.5 minutes).

**Estimated monthly request volume**

| Source | Cycles/hour | Requests/cycle | Requests/hour | Requests/month (×24×30) |
|---|---|---|---|---|
| Steam | 1 | 48 | 48 | **34,560** |
| Skinport | 4 | 1 | 4 | **2,880** |
| DMarket | 4 | 48 | 192 | **138,240** |
| **Total** | | | **244** | **~175,680** |

(The "API budget" framing in the brief is a Pricempire concept; for these three upstreams the relevant budgets are anti-abuse thresholds, not metered quotas.)

**Failure handling** (`collectors/base.py:99` `RateLimited`, `:154` `full_jitter_backoff`, ADR 006/013):

- 4xx other than 429 → `DECLINED`, no retry.
- 429 → AWS-style full-jitter exponential backoff, up to 5 attempts. In-call sleep capped at 60s; longer waits propagate via `RateLimited` and pause the source's APScheduler job (`scheduler.py:230` `_apply_pause`).
- 5xx / timeouts → same full-jitter backoff, 5 attempts, exhaustion returns `DECLINED`.
- After 429 retry exhaustion the scheduler computes a pause: use server `Retry-After` if present, else a doubling ladder `5 min → 10 → 20 → 40 → 60` (capped) within a rolling 1-hour window (`scheduler.py:184` `compute_pause_seconds`). State per-source in `_rate_limit_state`.
- **Cycle-level "soft-degrade" heuristic** (`scheduler.py:319`): if >50% of a cycle's items come back empty (DECLINED + ambiguous-None), all ambiguous-Nones are re-labeled `declined` so a Steam outage doesn't look like genuine "no listings."
- **Steam outlier filter** (ADR 006 §6, `collectors/steam.py:144`): rejects an observation below 20% of the item's 7-day Steam median, treating it as `DECLINED` rather than persisting the manipulation/fat-finger listing. Min 5 prior observations needed in the window.
- **DMarket title-mismatch guard** (ADR 012 §4): DMarket's `title=` is a loose substring; the collector compares the returned `title` against the requested name and skips with a WARNING when they differ. In the last 24h there were **726 such skips** (e.g. "Desert Eagle | Blaze" matched "Desert Eagle | Oxide Blaze", "MP9 | Hot Rod" matched "Souvenir MP9 | Hot Rod"). This is by design but is the dominant non-success signal in DMarket cycles — likely related to the per-cycle "8 unavailable" pattern (see §5).
- Errors **are not swallowed silently**; everything is logged at WARNING/ERROR with structured JSON-line format. The cycle wrapper at `scheduler.py:445` catches unhandled exceptions per cycle so APScheduler still sees a clean exit.

**API keys & auth posture**

- **Steam, Skinport, DMarket are all anonymous** — no API key, no header secret. There is no API key for any of these three in `.env` or anywhere in the code. The only secrets in `.env` are `POSTGRES_PASSWORD`, `SKIN_MARKET_API_TOKEN` (internal API bearer), and `DISCORD_BOT_TOKEN`.
- Steam will eventually rate-limit anonymous polling; ADR 006 has a plan to plug a `STEAM_SESSION_COOKIE` env into `make_client` when that becomes necessary. Not implemented yet.

---

## 4. Bot capabilities

**Entry point**: `python -m bot.main` (`bot/main.py:171`). discord.py 2.7.1; intents include `message_content` (privileged — must also be enabled in the Discord developer portal).

**Triggering** (`bot/main.py:115-119`):
- DMs to the bot
- @-mentions in guild channels
- All other messages are ignored. No passive listening (architectural rule, ARCHITECTURE.md §"out of scope").

**Access control** (`bot/main.py:122-135`): every author is checked against `DISCORD_ALLOWED_USER_IDS` (currently a single ID: `134481736472985601`). Empty allowlist → fail closed with a config-error message. Disallowed users get one "not authorized" reply per process lifetime; then suppressed.

**Slash commands** — None. The bot is a pure natural-language frontend driven by the LLM's tool-calling.

**Tools the LLM may call** — Seven, defined in `bot/tools.py` (`TOOL_DEFINITIONS` at line 603, `TOOL_FUNCTIONS` at line 804). Each is a thin httpx wrapper over the internal read API, with a "size-discipline" post-processing pass:

| Tool | Calls | Returns | Notes |
|---|---|---|---|
| `list_watchlist()` | `GET /items` | `{count, by_category, sample}` summary (NOT the raw 48-row list) | Categories inferred client-side from `display_name`. |
| `query_current_price(slug)` | `GET /items/{slug}/price` + `GET /items/{slug}/insights` | `{slug, display_name, per_source: [{source, denomination, state, price, volume, observed_at, minutes_since_observed}], anomaly_flag}` | Three-state per source: `fresh` (`< STALE_HOURS=4h`), `stale` (`> 4h`), `unavailable` (streak insight), `never_observed`. |
| `query_price_history(slug, source?, days=7, limit=500)` | `GET /items/{slug}/history` | Raw rows if ≤30; else per-source aggregate `{first/last/min/max/count}` (`HISTORY_DOWNSAMPLE_THRESHOLD=30`). | |
| `render_chart(slug, source='skinport', days=7)` | `GET /items/{slug}/chart` | PNG bytes returned as an `Attachment` dataclass, uploaded as a Discord file. | Matplotlib renders server-side. |
| `evaluate_deal(slug, amount, currency)` | `POST /deals/evaluate` | Verdict (`below_market`/`at_market`/`above_market`/`no_comparable_data`) + summary string. | API freshness threshold for "comparable" is `COMPARABLE_FRESHNESS_HOURS=4` (`api/routes/deals.py`). |
| `narrative_today()` | `GET /insights/narrative/latest` | The latest `daily_narrative` paragraph + a `{as_of, cited_count}` summary of citations. | Generated at 02:00 UTC by the analytics narrative job. |
| `whats_interesting(hours=6)` | `GET /insights/anomalies/recent` | Top-N anomalies by `|z|` if >10, else raw. | |

**Internal read API** (`api/main.py`) — every router behind `require_token` (bearer token via `Authorization: Bearer …`). `/health` is the lone exception. Endpoints in use by the bot:

```
GET /items
GET /items/{slug}
GET /items/{slug}/price
GET /items/{slug}/history
GET /items/{slug}/insights
GET /items/{slug}/chart       (returns image/png)
POST /deals/evaluate
GET /insights/narrative/latest
GET /insights/anomalies/recent
```

**LLM integration** (`bot/ollama_client.py`):

- Model: **`huihui_ai/Qwen3.6-abliterated:27b`** running locally on Ollama at `host.docker.internal:11434`. Same model and base URL also used by the analytics narrative job (`OLLAMA_MODEL` is shared via `.env`).
- Endpoint used: `ollama.AsyncClient.chat(model=…, messages=…, tools=TOOL_DEFINITIONS)` — i.e. the "Default" chat-completion path with `tools=` in the request, NOT Ollama's "Native" tool-calling variant. This is **load-bearing** for Qwen3-abliterated per ADR 016 and the project's persistent memory; the Native variant was found unreliable.
- Tool-use loop is capped at `MAX_TOOL_CALLS=5` rounds (`ollama_client.py:81`) to prevent runaway when the model keeps re-calling the same tool. If the loop exhausts, a canned fallback reply is sent (`ollama_client.py:342-348`).
- HTTP timeout to Ollama: `OLLAMA_TIMEOUT_SECONDS=300.0`. First call after model load is ~20-30s; subsequent calls under Ollama's KEEP_ALIVE are <2s. The 300s ceiling is defensive headroom for tail cases (history with months of data, narrative + many citations) — explicitly called a "band-aid" in the comment, with size-discipline in `bot/tools.py` being the real fix (the per-tool `_summarize_*` helpers).
- **No token-usage instrumentation.** Cost is not tracked (local Ollama, so no $ cost, but no latency histograms, no input/output token counts).

**System prompt** — `bot/system_prompt.py` (single triple-quoted string, ~120 lines). Key features:
- Explicit `# CRITICAL: never answer from memory` block (lines 22-33) telling the model that every factual claim about CS2 prices must come from a tool call.
- Trigger-phrase lists per tool (e.g. "what's the price of X?", "how much is X?", …) — open-source models need more steering than cloud APIs per ADR 016.
- Hard-coded slug rules ("AK-47 | Redline (Field-Tested)" → "ak-47-redline-field-tested", `★` → `star-`, `™` → empty, `Souvenir` prefix preserved).
- A denomination-discipline rule: never collapse `usd` and `wallet_credit`; "SC" footnote first time wallet-credit is mentioned in a reply.

**Defensive handling of bad LLM output** (`ollama_client.py:119-200`): malformed `arguments` (JSON-string vs dict), unknown tool names, missing/extra kwargs, and tool exceptions are all caught and converted into `tool_result` strings fed back into the conversation rather than crashing.

**Broken / disabled / vestigial**

- `docs/archive/bot_skill_hermes_attempt/` contains the Phase 7b "Hermes skill" — superseded by the in-process Ollama-tool approach in `bot/`. ARCHITECTURE.md still references a `bot_skill/` directory at line 105-107 that no longer exists at the repo root.
- The "Hermes Discord bot" arrow in the ARCHITECTURE.md architecture ASCII diagram is therefore stale — the bot now lives inside compose, not as a `~/.hermes-discord/` host process. ADR 014 §10 mentions this Phase 7c collapse but the architecture diagram wasn't updated.
- No slash commands. Listed as "out of scope for v1" implicitly — `/predict` is referenced in ARCHITECTURE.md as a post-v1 idea.

---

## 5. The 4-hour staleness issue

> **Resolved 2026-05-15** (Phase 1 → Phase 2a). The timestamp-split work is now complete across both API endpoints that gate on freshness. `/items/{slug}/price` (Phase 1) and `/deals/evaluate` (Phase 2a) both drive off `observation_log.last_observed_at` for the freshness decision and expose `last_polled_at` + `last_changed_at` to consumers. The bot renders 🟡 stale only when `last_polled_at` is genuinely old; `last_changed_at` is informational and explains "price hasn't moved in Nh" without escalating to a warning. ADR 017 has the design. The §5 audit text below is preserved verbatim as the historical record of the bug.

**The root cause is not under-polling. It's a dedup-vs-display mismatch.**

### What the brief calls "4+ hour stale"

The bot's `query_current_price` tool labels a source as `stale` (with the 🟡 prefix per `bot/system_prompt.py:99`) when `minutes_since_observed > STALE_HOURS*60` where `STALE_HOURS=4` (`bot/tools.py:57`). The `minutes_since_observed` is computed from `row["observed_at"]` in `bot/tools.py:226-227`. That field comes from the API.

### What `observed_at` actually is

The API's `/items/{slug}/price` endpoint (`api/routes/items.py:108-122`) selects:

```sql
SELECT DISTINCT ON (p.source_id)
    ...
    p.timestamp AS observed_at
FROM prices p
JOIN sources s ON s.id = p.source_id
WHERE p.item_id = :item_id AND s.enabled = TRUE
ORDER BY p.source_id, p.timestamp DESC
```

So `observed_at` is **the timestamp of the most recent row in `prices` for that `(item, source)`** — *not* the most recent poll.

### Why those diverge — the dedup gate

`collectors/base.py:352` `should_write_observation` returns `False` when the latest `prices` row for the same `(item, source)` has the same `(price, volume)`. This is by design (ADR 009 §3 — "tolerances are an arbitrary bug source; cent-level changes are real signal"). The cycle counter logs ~30-45 `unchanged` per Skinport cycle and ~30-40 `unchanged` per DMarket cycle as steady-state — most items don't move every 15 minutes.

Meanwhile `observation_log` is upserted unconditionally on every successful poll (`collectors/base.py:252` `update_observation_log`, called *before* the dedup check at `collectors/scheduler.py:367`). So the database has a fresh "we saw this item N minutes ago" signal for every item we successfully polled. **The API just doesn't read it.**

### Empirical evidence (queries against the live DB at 2026-05-15 ~20:45 UTC)

Latest `prices` row vs latest `observation_log` row for the same `(item, source)`, restricted to pairs where the price row is ≥ 4h old:

```
              market_hash_name             |     source    |  latest price ts  |  observation_log  |  gap (obs - price)
-------------------------------------------+---------------+-------------------+-------------------+--------------------
 ★ Moto Gloves | Spearmint (Field-Tested)  | dmarket       | 2026-05-12 03:16  | 2026-05-15 20:40  | 3d 17h
 M4A4 | Howl (Factory New)                 | dmarket       | 2026-05-13 11:40  | 2026-05-15 20:39  | 2d 08h
 ★ Karambit | Fade (Factory New)           | skinport      | 2026-05-15 04:23  | 2026-05-15 20:38  | 16h
 AWP | Dragon Lore (Field-Tested)          | skinport      | 2026-05-15 04:23  | 2026-05-15 20:38  | 16h
 ... 16 more rows
```

These are all items the collector *is* polling on schedule — the `observation_log` says we saw them within the last few minutes — but whose prices have genuinely been flat, so no new `prices` rows get written, so the API surfaces them as "21h ago" / "16h ago" / multi-day stale.

Staleness buckets across the latest `prices` timestamp per `(item, source)` pair:

| Source | items < 1h | items 1-4h | items > 4h |
|---|---|---|---|
| dmarket | 10 | 14 | **16** |
| skinport | 9 | 13 | **25** |
| steam_market | 20 | 11 | **8** |

Skinport in particular has 25/48 items where the API will report "> 4h ago"; the bot will render those as 🟡 stale even though Skinport is being polled cleanly every 15 minutes. The cycle log confirms:

```
20:08:38 Skinport cycle complete: 48 attempted, 6 written, 41 unchanged, 1 unavailable
20:23:38 Skinport cycle complete: 48 attempted, 3 written, 44 unchanged, 1 unavailable
20:38:38 Skinport cycle complete: 48 attempted, 2 written, 45 unchanged, 1 unavailable
```

41-45 `unchanged` per cycle is the dedup mechanism quietly working.

### Is polling actually running?

Yes. Last-24h cycle counts from the collector log:
- Skinport: cycles every 15 minutes, **0 failures, 0 retries**, 1 `unavailable` per cycle (one item legitimately absent from Skinport's bulk response — almost certainly `M4A4 | Howl (Factory New)`).
- DMarket: cycles every 15 minutes, **0 failures, 0 retries**, 8 `unavailable` per cycle. The 8 is suspicious; it's exactly the count of items that fall through DMarket's title-mismatch guard each cycle (ADR 012 §4). The collector logs 726 `DMarket title mismatch` warnings in the last 24 hours = ~7.6 per 15-min cycle.
- Steam: cycles every 60 minutes, ~22-25 `written`, ~10-15 `unchanged`, ~10-13 `unavailable`. The 8-13 unavailable Steam items per cycle are real "Steam has no listings" responses (`success:false`) and include rare items like `M4A4 | Howl (Factory New)`, `★ Sport Gloves | Pandora's Box (Field-Tested)`, etc.

The most recent rate-limited event was at 2026-05-14 10:00 UTC on Steam (job paused 1200s). No 429s or exhausted-retry events in the last 24h.

### So is anything actually broken?

Two genuinely-stale items unrelated to the dedup story:

- `Glock-18 | Fade (Factory New)` on Steam: `observation_log` shows last successful observation at 2026-05-14 13:21 — **31 hours stale even by observation-log measure**. Steam has consistently returned `success:false` for this item. Genuinely "Steam has no listings."
- `★ Butterfly Knife | Marble Fade (Factory New)` on Steam: observation_log at 2026-05-15 18:22, ~2h ago. Borderline.

DMarket coverage: only 40/48 items have any observation_log row at all (some of the perennially-title-mismatched items never produce a PriceObservation, so `update_observation_log` is never called for them).

### Concrete summary

1. **Polling is running on schedule.** No silent failure, no swallowed errors. Cycle complete lines are consistent every 15m / 60m.
2. **The 4-hour bot warning is a display bug, not a freshness bug.** The API's `observed_at` should be `observation_log.last_observed_at` (last successful poll), and `prices.timestamp` should be exposed as a separate "last_changed_at" field. With that split, the bot's `STALE_HOURS=4` threshold can be applied to genuine poll freshness, and "this price hasn't moved in 16h" becomes a separate, useful signal rather than confusion.
3. **DMarket's "8 unavailable per cycle" is mostly the title-mismatch guard firing on items DMarket aliases (Souvenir/StatTrak variants).** Worth widening the matcher or maintaining a per-item override map.
4. **`★ Moto Gloves | Spearmint (Field-Tested)` on DMarket has NO `observation_log` row at all** (3+ days since the last `prices` row, no observation since). It is one of the items DMarket apparently doesn't list or only lists under a fuzzed title. Worth confirming whether DMarket has it at all.

Locations to look at if implementing a fix:
- `api/routes/items.py:108-125` — the query that produces `observed_at`. Add a join on `observation_log` and surface both timestamps.
- `api/schemas.py:58-69` `PerSourcePrice` — add a `last_polled_at: datetime` field alongside `observed_at`.
- `bot/tools.py:226-242` and `bot/system_prompt.py:99-103` — switch the freshness decision to the new `last_polled_at`; keep `observed_at` for "price last moved at."

---

## 6. What's missing or rough

**Observability**
- All logs are structured JSON-lines but go only to container stdout (Docker driver). No log shipping, no metrics endpoint, no Prometheus scrape target, no Grafana, no Sentry.
- No latency or token-usage instrumentation on the Ollama path (a slow round-trip is invisible until users complain).
- API has no per-route metrics — no `prometheus-fastapi-instrumentator`, no access log filter.
- No alerting if Steam pauses for 1200s; only the log line tells you.

**Bot-level production gaps**
- No per-user rate limiting. A single allowlisted user can hammer the bot and force serial Ollama calls; nothing returns "slow down."
- No conversation context across messages. Every Discord message starts a fresh `[system, user]` pair (`bot/ollama_client.py:222-225`) — by design, but users will likely ask "and what about the Karambit?" expecting context, and get re-prompts.
- "Disallowed user told once per process" state (`bot/main.py:93` `suppressed_users`) resets on every container restart. Not a real problem; just noting.
- The `discord.py` event loop has no health/heartbeat endpoint. If the bot wedges, you find out from Discord users.

**Data layer**
- No retention / no compression policy on the `prices` hypertable. TimescaleDB compression and `add_retention_policy(...)` are both unused (no `policy_compression`, no `policy_retention` jobs).
- `insights` is a regular table, not a hypertable. At 34k rows after 4 days it'll be ~1M rows in 4 months — should probably also be a hypertable or have an explicit cleanup job, but performance is fine for now.
- `prices.raw_response` stores the full upstream JSON per row. For Skinport this is small (per-item slice). For Steam it's tiny. For DMarket it's the cheapest-offer + objects count — also small. Worth re-checking sizes after a few months.

**Collectors**
- Watchlist rotation `TODO` at `collectors/scheduler.py:178` — fine while the watchlist stays ≤50.
- `_run_named_source` swallows all exceptions (`scheduler.py:445`) — by design for APScheduler resilience, but a crash in a collector subclass produces only a log line. If the operator isn't watching, they won't know.
- DMarket coverage gap (8 items lost per cycle to title-mismatch) is the most fixable real issue. Either a tighter matcher, a per-item alias map in the watchlist YAML, or both.

**API**
- The `observed_at` ambiguity (§5) is the biggest API debt.
- No `/insights/narrative/history` or paged narrative endpoint. The bot can only see the latest. Fine for v1.
- Charts are generated synchronously per request; no caching of identical recent renders. At v1 traffic, fine.

**Auth / secrets**
- `.env` carries plaintext secrets including `DISCORD_BOT_TOKEN` and `POSTGRES_PASSWORD`. Not committed (`.gitignore` excludes it) but not encrypted at rest either. This is consistent with `ARCHITECTURE.md`'s "v5+" stance on user accounts but worth keeping in mind for the eventually-public phase.
- `SKIN_MARKET_API_TOKEN` is a single static bearer reused by `bot` and any future host caller. No rotation tooling beyond "edit `.env` and restart `api` + `bot`" (documented in `docs/operations.md`).

**Tests**
- `tests/` has 13 test modules covering collectors, the bot, scheduler, DB roundtrip, migration roundtrip, API, naming, watchlist edit, analytics, and smoke. Destructive tests (migration roundtrip) are excluded by default — opt in with `pytest -m destructive`. Not audited for coverage; visual inspection of names suggests it's reasonable for v1.

**Docs**
- 16 ADRs in `docs/adr/` — thorough.
- `ARCHITECTURE.md` architecture diagram still shows "Hermes Discord bot" as a separate host process; the current bot is a compose service. Cosmetic.

---

## 7. Open questions for a human

1. **Is DMarket actually expected to cover all 48 watchlist items?** 8 are perennially lost to the title-mismatch guard (e.g. "MP9 | Hot Rod" → "Souvenir MP9 | Hot Rod", "★ Karambit | Doppler" → "★ StatTrak™ Karambit | Doppler"). Some of these (gloves variants, very-rare knives) may simply not be on DMarket — but the loose-substring fallback is hiding that signal.
2. **What is the user-facing definition of "stale"?** "Price hasn't moved in 4h" (current API behavior) and "we haven't successfully polled the source in 4h" (probably the intent) are different facts. The fix in §5 depends on which one the bot is meant to show — possibly both.
3. **Is the bot supposed to maintain conversation context?** Single-turn-only is a defensible choice but it's not called out in `ARCHITECTURE.md` explicitly.
4. **Does the user want any retention/compression configured before the `prices` hypertable hits "annoying" size?** TimescaleDB makes this trivial later but not free (re-chunking costs time).
5. **Is there a plan to validate Skinport's `quantity` field as `volume`?** Per ADR 008 it's listings count, NOT 24h sales — different from Steam's `volume` field. The unified `prices.volume` column is overloaded; the bot's renderer assumes "listings" but the analytics layer treats Steam volume as sales for the volume-anomaly insight.
6. **What's the planned shape of CSFloat integration (v2)?** Float-tier pricing implies per-listing observations, which doesn't fit the current `(item_id, source_id, timestamp)` PK. Will be a schema change.
7. **Should `daily_narrative` be re-runnable manually?** Only 4 rows exist; if 02:00 UTC fails (e.g. Ollama down) you'd want a `python -m analytics.narrative --once` rerun path. The narrative module supports `--help` style invocation but I didn't trace the CLI.
8. **Is the bot's "out of scope" list (ARCHITECTURE.md §149-178) intended to be enforced by code or by convention?** Currently it's convention only — nothing prevents adding a passive message logger.
