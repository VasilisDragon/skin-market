# ADR 015 — Hermes bot skill design

**Status:** Accepted
**Date:** 2026-05-13
**Related:** ADR 010 (analytics), ADR 013 (rate-limit policy), ADR 014 (read API; §3 → §10 → §10-multi-token correction history), `docs/sources-and-semantics.md`

## Context

Phase 7 closes the data → bot loop. The skin-market pipeline (collectors → Postgres → analytics → FastAPI read API) accumulates and exposes price data; this ADR pins how the Discord bot — a Hermes skill — reads from it and renders for human users without breaking the architecture's load-bearing invariants (denomination tagging, no collapsed prices, "I don't track that" rather than live scraping).

The phase split:

- **7a** — `analytics/unavailability_streak.py` + observation_log table (the bug fix found mid-phase, see §"Unavailability streak" below) + two new API endpoints (`/insights/narrative/latest`, `/insights/anomalies/recent`).
- **7b** — `bot_skill/{tools.py, SKILL.md, README.md}` + multi-token auth refactor + this ADR.

## Decisions

### 1. Bot scope: seven tools, watchlist-write deferred

| Tool | Endpoint(s) called | Notes |
|---|---|---|
| `list_watchlist` | `GET /items` | Direct passthrough. |
| `query_current_price` | `GET /items/{slug}/price` + `GET /items/{slug}/insights` | Composes both into a three-state per-source render (§4). |
| `query_price_history` | `GET /items/{slug}/history` | Defaults to 7d / 500 row cap; passes through query params. |
| `render_chart` | `GET /items/{slug}/chart` | Returns `Attachment(content=bytes, media_type, filename)` — single-source by design. |
| `evaluate_deal` | `POST /deals/evaluate` | Passes through the verdict + `summary`; bot renders the latter verbatim. |
| `narrative_today` | `GET /insights/narrative/latest` | Latest daily narrative paragraph; 404 → "no narrative yet". |
| `whats_interesting` | `GET /insights/anomalies/recent?hours=N` | Currently-firing divergences + volume anomalies, joined with item metadata. |

**Watchlist add/remove via bot is explicitly deferred to v5+** alongside user-tier auth (ARCHITECTURE.md "Things explicitly out of scope for v1"). The operator path is `scripts/watchlist_edit.py`. The bot tells users this in its error response when an unknown item is requested — surfacing the right channel rather than failing silently or attempting live scraping (the latter would also violate the ARCHITECTURE.md anti-pattern).

### 2. Denomination rendering — mandatory, every price, every reply

The architectural invariant from `docs/sources-and-semantics.md` is enforced at the API boundary (ADR 014 §2) and re-enforced at the bot rendering layer here:

- `usd` → render as `$X.XX USD`.
- `wallet_credit` → render as `X.XX SC` (SC = Steam Wallet credit).
- On first introduction in a reply, add the one-line footnote: *"SC = Steam Wallet credit; carries a structural ~30–50% premium over USD because it can't be withdrawn."*

The bot must NEVER:
- Average prices across denominations (the architecture refuses this by construction).
- Render `$42.92 on Steam` without the `SC` qualifier.
- Drop a source's price because "the spread looks weird" — Doppler knives showing 8× between sources is a real signal (different phases), not a data error.

SKILL.md is the canonical document Hermes' LLM reads at session start; this ADR is the engineering rationale. Both must stay in sync.

### 3. Three-state availability render

`query_current_price` returns one `per_source` entry for each `EXPECTED_SOURCES` value, classified into one of four states:

- **`fresh`** — observation in the last `STALE_HOURS` (4h, matching `COMPARABLE_FRESHNESS_HOURS` in `api/routes/deals.py`).
- **`stale`** — observation older than 4h. Render with 🟡 prefix.
- **`unavailable`** — no observation AND a streak insight exists (`value=N`, `meta.last_seen_observed=<timestamp or null>`). Render "*Steam: unavailable for last N cycles (last seen 4h ago at 44.53 SC)*".
- **`never_observed`** — no observation, no streak insight. Render "*no observation yet*". Distinct from unavailable so the bot doesn't lie during post-deploy warmup before the first analytics cycle has run.

**Why a separate `never_observed` state** — without it, every newly-deployed system would show "unavailable for ? cycles" for items the streak compute hasn't seen yet. That's misleading. The four-state design lets the bot stay honest during the warmup window without false alarms.

**All three sources rendered every time, regardless of state.** Silently omitting a source hides information — the user can't distinguish "Steam doesn't have it" from "the bot forgot about Steam".

The 🚨 anomaly annotation fires when a `cross_source_divergence` insight for the item has `computed_at` inside the last `ANOMALY_FRESHNESS_HOURS` (2h). Older divergences are stale (z-scores reset hourly with each analytics cycle).

### 4. `unavailability_streak` insight type — design + mid-phase bug

User asked for a per-(item, source) streak counter — "how many consecutive analytics cycles has Steam been missing this item?" — additive to the existing collector-cycle `unavailable` vs `declined` semantics (ADR 013) rather than replacing them.

**The intended algorithm:** for each enabled source, find each item's last successful observation. If older than `source.interval_minutes × 1.5` (grace), the pair is "missing this cycle". Look up the previous streak insight; if `meta.last_seen_observed` matches the current state, increment streak; otherwise reset to 1. Skip emit for currently-observed pairs (sparse storage).

**The bug found in live verification:** the first draft read `MAX(prices.timestamp)` per pair. But `prices` is *dedup-filtered* — `should_write_observation` skips writes when `(price, volume)` is unchanged (ADR 009 §3). Stable-price items have a `prices.timestamp` that doesn't advance, so the streak compute interpreted "dedup'd successful observation" as "missing observation". Live one-shot returned 46/48 Skinport items as missing while the actual cycle shape was "47 observed, 1 unavailable".

**The fix (committed in 7a):** new `observation_log(item_id, source_id, last_observed_at)` table, composite PK, upserted on every `PriceObservation` yielded *pre-dedup* by the collector. The streak compute reads from `observation_log` instead of `prices`. Migration 0004 backfills `observation_log` from the latest `prices.timestamp` per pair so the first analytics cycle post-deploy doesn't see everything as suddenly-missing.

The `test_dedup_observation_counts_as_fresh` regression test in `tests/test_analytics.py` directly exercises this — old `prices.timestamp` + fresh `observation_log` → must emit NO streak row. It would have failed against the first draft; passes against the fix.

**Why `observation_log` generalizes** — any future analytics that needs "did source X successfully poll item Y recently?" has the right signal now. Streak compute is the first consumer; not the last.

### 5. Hermes integration shape — plausible-shape guess

We don't have a working Hermes skill to pattern-match against. The choices that landed in 7b are the most general guesses:

- **Plain Python functions** with type hints and rich docstrings. A loader can introspect either via `inspect` or via the `TOOLS` registry list.
- **No-op `@tool` decorator** — appends the function to module-level `TOOLS`. If Hermes wants per-tool metadata (description, JSON schema), attach it here once the loader's needs are known.
- **Plain return values** — `dict`, `list`, `str`, or an `Attachment` dataclass for binary content. The bot's reply layer (Hermes' LLM + Discord serializer) renders these per SKILL.md.
- **Typed exceptions** — `SkinMarketBotError` and four named subclasses (`ApiUnreachableError`, `ApiAuthError`, `ItemNotInWatchlistError`, `ApiUnexpectedError`). The reply layer matches exception type → user-facing message per SKILL.md's error matrix.

**If Hermes' loader rejects this shape**, the refactor is in `tools.py` only — the function bodies (HTTP calls, response shaping) don't depend on the registration mechanism. SKILL.md and ADR 015 are authoritative on rendering; how those tools are surfaced to the LLM is the loader's concern.

`test_every_tool_has_docstring` in `tests/test_bot_skill.py` guards against docstring-drop regressions — many tool loaders introspect docstrings for the LLM's tool-choice prompt, so an empty docstring breaks discoverability silently.

### 6. Auth model — multi-token by design, single-token in v1

ADR 014 §3 originally posited "no auth, compose network is the gatekeeper". Phase 7 inverted that when Hermes turned out to be a host process at `~/.hermes-discord/`, not a compose service. §10 (Phase 6.6) added a single static bearer token. **§10 is now amended (Phase 7b) so the underlying check is set-membership rather than single-value comparison:**

- `SKIN_MARKET_API_TOKENS` (plural, comma-separated) — canonical knob.
- `SKIN_MARKET_API_TOKEN` (singular) — convenience alias for the single-consumer case. Both env vars are read; the **union** is the accepted set.
- v1 ships with one token, set via the singular alias. README.md's install recipe uses the singular form for clarity.
- When a second consumer arrives (operator CLI, future SaaS), it's a `.env` edit + `docker compose up -d api` — not an auth refactor.

The check itself is `secrets.compare_digest` against each token in the set; constant-time per comparison. Iterating the set leaks set size to a sufficiently patient attacker but not individual tokens — fine at v1's 1–3 consumer scale.

**Why not just defer multi-token entirely:** the bot's README ends up shaping the operator's mental model of "this is auth". If v1 hardcodes a single-token API and v2 has to refactor to a token set, every operator doc rewrites. Doing the right shape now is a one-line parser change (set parsing instead of string comparison) and prevents the future churn.

### 7. Token rotation workflow

No rotation tooling in v1. The README documents the manual recipe:

1. Generate the new token: `openssl rand -hex 32`.
2. Add it alongside the old one: `SKIN_MARKET_API_TOKENS=<old>,<new>`.
3. `docker compose up -d api` — api now accepts both.
4. Update the bot's `SKIN_MARKET_API_TOKEN=<new>` in Hermes' env, restart Hermes.
5. Remove the old token from `SKIN_MARKET_API_TOKENS`; `docker compose up -d api`.

This is graceful — there's never a window where the bot's token doesn't authenticate.

### 8. Error matrix — typed exceptions, not error strings

The tools raise typed exceptions; SKILL.md's error matrix tells the LLM exactly how to render each. **The bot does not parse error strings** — that's the difference between an exception-based contract and a string-based one. If the message in `ItemNotInWatchlistError` ever changes, the bot's user-facing reply doesn't break because the exception *type* is the contract.

The matrix:

| Exception | User-facing rendering |
|---|---|
| `ItemNotInWatchlistError` | "I don't track that item yet — operator path is `scripts/watchlist_edit.py add`." |
| `ApiUnreachableError` | "Market data service is unreachable; try again in a moment." |
| `ApiAuthError` | "Auth misconfigured between me and the market service." |
| `ApiUnexpectedError` | "Unexpected response — try again; if persists, see error detail." |

Two special cases:
- `narrative_today()` raising `ItemNotInWatchlistError` re-purposes the 404 path for "no narrative yet" — SKILL.md tells the LLM to render this as "*No daily summary yet — narrative job runs at 02:00 UTC*" rather than the generic item-not-tracked message.
- `evaluate_deal` returning `verdict: "no_comparable_data"` is NOT an exception — it's a normal response with a specific verdict. The bot renders the `informational` block and tells the user a comparison isn't possible in their currency.

### 9. Stale-data warning composition

If `query_current_price` returns a `per_source` list where every entry is `stale` or worse (no `fresh` rows), SKILL.md instructs the LLM to prefix the reply with `⚠️ All sources are stale (>4h old); the collector may be paused or rate-limited.` This handles the rare case where every source has fallen behind (e.g. all three sources rate-limited simultaneously) — the user shouldn't act on a recommendation built from cold data.

## Consequences

- **Pro:** the bot inherits the API's architectural invariants by construction. Every price the user sees carries denomination context; no source is silently dropped; the streak counter surfaces "Steam: unavailable for 3 cycles" rather than the API quietly omitting Steam.
- **Pro:** the three-state availability design distinguishes warmup from unavailability honestly. New deployments don't lie during the first analytics cycle's worth of data.
- **Pro:** typed exceptions + a separate SKILL.md make the error-rendering layer LLM-driven without coupling bot replies to specific error strings. Refactoring tool internals doesn't change user-facing prose.
- **Pro:** the multi-token auth design accepts a second consumer without a refactor — operator CLI, future SaaS, or a second bot environment all join the set via `.env` edit.
- **Con:** Hermes integration shape is a guess. If the loader expects a different registration pattern (e.g. dict-based tool definitions, JSON schema arguments), tools.py needs a small refactor. Function bodies are independent of that; SKILL.md is authoritative on rendering.
- **Con:** `EXPECTED_SOURCES` is hardcoded in `tools.py` (matching the v1 source set). When a fourth source lands, it's a two-line edit (`EXPECTED_SOURCES`, `_DENOMINATION_BY_SOURCE`) — but it's an edit. A future `/sources` API endpoint would let the bot discover sources at startup, eliminating the hardcoded list. Out of scope for v1.
- **Con:** the bot has no caching layer. Each user query reads through to the API → DB. At Discord's interactive scale (low single-digit QPS per bot) this is fine; if a future use case fan-outs queries, an in-process LRU on per-tool calls is the right addition.
- **Con:** no in-bot watchlist editing means a user who wants a new item tracked has to ping the operator. That's the right v1 trade-off (no multi-user auth) but worth flagging as the friction point most likely to drive a v2 request.

## What this ADR does NOT decide

- **The exact wording of every user-facing message** — SKILL.md is authoritative there and will iterate as we see actual Discord interactions. This ADR's matrix is the contract shape, not the prose.
- **Whether to add a `/sources` endpoint** — listed as a future enhancement; not blocked by anything in 7b.
- **The bot's behavior in non-English contexts** — v1 ships English-only.

## ADR 014 §3 → §10 → §10-multi-token correction history

For posterity:

- **§3 (initial)** — "No auth, compose network is the gatekeeper." Assumed bot would be a compose service.
- **§10 (Phase 6.6)** — Bot turned out to be a host process. Added single static bearer token + `127.0.0.1` port mapping. SKIN_MARKET_API_TOKEN env var.
- **§10 (Phase 7b, this ADR)** — Set-membership check (multi-token) so adding consumers is a config edit, not an auth refactor. SKIN_MARKET_API_TOKENS (plural) added as canonical; singular alias preserved for the v1 single-consumer case.

The deployment dependency that ADR 014 §3 made explicit ("if Phase 7 deploys the bot outside compose…") did not hold; the correction is documented in §10. Future ADRs that depend on "the bot runs at X" should make that assumption explicit so the same chain of corrections doesn't have to be re-discovered.
