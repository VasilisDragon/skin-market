# ADR 019 — Pricempire collector design

**Status:** Accepted
**Date:** 2026-05-16
**Related:** ADR 009 (scheduler design, dedup-on-write), ADR 017 (timestamp split), ADR 018 (Pricempire as breadth source), Phase 2a (`collectors/pricempire.py`, migrations 0005 + 0006)

## Context

ADR 018 establishes Pricempire as a breadth-coverage data source
layered on top of the curated Steam/Skinport/DMarket collectors,
backed by the `pricempire_observations` hypertable. This ADR
documents the collector module that performs the ingest.

The shape constraints:

- One HTTP call returns the whole catalog (~39k items, ~64 MB at our
  six-source filter). No pagination, no per-item endpoint.
- Each item carries up to six nested price rows
  (`buff163` / `buff163_buy` / `skinport` / `dmarket` / `csmoney` /
  `swapgg`). Per-row fields: `price` (cents), `count`, `updated_at`,
  `last_checked_at`, `provider_key`, optional `meta` (e.g.
  exchange rate for `buff163`'s CNY price).
- Pricempire wire prices are integer cents (e.g. 17316 for $173.16).
  Skinport rows carry a placeholder `updated_at = "2025-01-01..."`
  while `last_checked_at` is real-time.
- 15-minute scheduled cadence (the `pricempire` pseudo-source's
  `interval_minutes`). One in-flight cycle at a time
  (`max_instances=1` via APScheduler defaults).

## Decisions

### 1. Single `collect_snapshot()` entry point; deliberately NOT a `BaseCollector` subclass

The existing `Collector` abstraction in `collectors/base.py` is
designed for per-item-per-source ingest: `collect_one(client,
market_hash_name) -> PriceObservation | None | DECLINED`, plus a
`collect_cycle()` helper that iterates a watchlist with
`inter_request_delay` between items. Pricempire's shape — one
HTTP call yielding many rows — doesn't fit cleanly. Two options
considered:

- **Force the abstraction.** Pretend each item is a separate
  `collect_one()` call backed by the same in-memory snapshot.
  Awkward — `collect_one()` would need to do per-item dispatch
  inside an iterator the BaseCollector doesn't know about, the
  HTTP layer would lie about request count, and any rate-limit
  logic (which Pricempire doesn't expose via headers anyway) would
  fire under the wrong assumptions.
- **Skip the abstraction.** Pricempire gets a standalone module
  with one public entry point: `collect_snapshot()`. The scheduler
  invokes it directly through a small pseudo-source code path
  (ADR 018 §3).

Option 2 wins. The `BaseCollector` abstraction is for per-item
upstreams; a bulk-snapshot upstream is structurally different and
deserves its own shape rather than a leaky one. Future per-item
sources continue to use `BaseCollector`; future bulk-snapshot
sources will follow the Pricempire pattern. The
`_PSEUDO_SOURCES` set in `scheduler.py` is the documented bypass
list.

### 2. Stream-parse the response via `ijson` over `BytesIO`

The full response is 64 MB raw bytes. Loading it as a Python
`dict` peaks resident memory at ~150 MB (most of which is the
~234k-row nested `prices` arrays). The collector container runs
alongside the scheduler and the analytics services; a 150 MB
per-cycle spike per source is workable on a DGX Spark but is
unnecessary.

`ijson.items(stream, "item", use_float=True)` streams the
top-level array element-by-element, so the Python-object peak
stays in the low MB range. The 64 MB raw bytes are still held in
memory (we read the response in full via `response.content` →
`BytesIO`); we could trade that for true byte-streaming but the
ijson backend's iterable-bytes path is fragile across versions, and
64 MB is well within budget. The win we actually need is on
parsed-object memory.

`use_float=True` is load-bearing: ijson's default decoder maps JSON
floats to `Decimal`, and `Decimal` is not JSON-serializable by
psycopg's default JSON encoder. We round-trip the wire row into
the `raw_response` JSONB column unchanged; if the row carries
Decimal values (e.g. `liquidity = 62.802508437142585` or
`meta.rate = 0.1468709151526723`) the insert blows up mid-stream.
`use_float=True` returns native Python floats, which JSON-encode
cleanly. The first live cycle exhibited this bug after 14 items;
the regression test pins the fix.

### 3. Price-cents parsing through `Decimal(str(...))`

Wire prices are integer cents today (e.g. `17316`). The natural
parse is `Decimal(int(raw_price)) / 100`. But `int()` truncates
floats (`int(173.16) == 173`), so the natural parse silently
loses precision if Pricempire ever changes the wire format to
floats.

Defensive parse: `Decimal(str(raw_price))` preserves precision
across int and float inputs. `Decimal("17316") / 100 == 173.16`;
`Decimal("173.16") / 100 == 1.7316`. The first case is what we
expect today; the second would fail loudly if Pricempire ever
shipped a wire-format change (we'd see decimal-point shifts in
the data and notice).

### 4. Dedup gate compares against `pricempire_observations` itself

Same shape as `collectors.base.should_write_observation`: skip
insert when `(price, count)` matches the latest existing row for
the same `(item_id, source_id)`. Phase 1's dedup-vs-display
learning (ADR 017) applies, but Phase 2a does NOT add an
`observation_log` analog for Pricempire — Phase 2b's drift
detection will decide whether one is needed.

Until then the dedup gate operates against
`pricempire_observations` itself. The query is one indexed
lookup per row (`ORDER BY timestamp DESC LIMIT 1` on the composite
PK), small enough to run per row without batching.

### 5. Unknown items skipped, unknown providers logged once per cycle

Pricempire returns the whole 39k-item catalog; the collector
persists rows only for items in our `items` table. A
`canonical_name → item_id` map is built once per cycle from
`SELECT id, market_hash_name FROM items`. Items not in the map
get counted as `items_skipped_unknown` and surface in the
cycle-complete log line. Phase 2b adds the long-tail layer for
items outside the watchlist.

Unknown `provider_key` values (not in
`_PROVIDER_KEY_TO_SOURCE_NAME`) get counted and logged ONCE per
cycle (as a sorted list) rather than per row. Pricempire may add
providers without warning; the warning surfaces the new wire keys
operators need to add to the mapping + sources table.

### 6. Failure handling: log WARNING and exit; next cycle is the retry

- `httpx.HTTPStatusError` (4xx / 5xx) → log the status + URL,
  return without raising. The 15-min APScheduler tick is the
  retry budget.
- `httpx.RequestError` (transport / timeout) → same.
- `PRICEMPIRE_API_KEY` empty → log ERROR and return immediately
  (no HTTP call). Fail-fast guard: a misconfigured deploy that
  silently runs zero-effective cycles for hours is the worst
  failure mode; the fail-fast pattern surfaces it on the first
  cycle.
- Unexpected exception mid-stream → log with traceback, return.
  The scheduler keeps the source's next cycle scheduled; the
  operator gets a per-item-count progress indicator in the log
  even on partial failure.

No in-call retries. Retrying a 64 MB bulk call inside a single
cycle window is wasteful — if Pricempire is rate-limiting us, the
correct response is to back off until the next 15-min tick.

### 7. Cycle-complete log line mirrors the per-item collectors

```
Pricempire cycle complete: 39392 items seen, 39344 skipped (not
in watchlist), 281 rows written, 7 unchanged, 0 skipped (unknown
provider), elapsed 4.7s
```

Same shape as `Skinport cycle complete: 48 attempted, 6 written,
41 unchanged, 1 unavailable`. Operators reading the structured
JSON log don't need to context-switch when scanning for the
per-cycle summary across sources.

### 8. Per-item commit cadence

The collector commits the session after each item's prices array
is processed (not per row). Two reasons:

- A mid-cycle SIGKILL keeps partial progress to the last
  committed item, matching the per-item collectors' commit
  cadence and the docker-compose graceful-shutdown story.
- An operator can watch the table grow live with `\watch` in
  psql, which is how the first live cycle was verified.

We don't commit per row because the per-item commit already gives
that property at finer granularity than needed (six rows per
item × 48 items = 288 rows ≈ 50 commits per cycle, dominant by
the items-seen count); per-row commits would just add round-trip
overhead with no progress-safety win.

## Rejected alternatives

- **httpx streaming via `response.iter_bytes()` directly into
  `ijson.items(...)`.** The ijson backend's iterable-bytes path is
  fragile across versions; we hit a "too many values to unpack"
  error on the first attempt. Switching to `BytesIO(response.content)`
  keeps the Python-object streaming win (which is the actual
  bottleneck) and trades only the raw-bytes memory savings (which
  we don't need).
- **A per-cycle observation_log analog.** Deferred to Phase 2b
  alongside drift detection. Premature here — we don't have a
  Phase 2a consumer of "did Pricempire poll source X for item Y
  recently?" yet.
- **Custom JSON encoder for Decimal → string on insert.** Considered
  before `use_float=True` was the cleaner fix. JSON encoders are a
  global setting in psycopg's typed-cursor path; configuring per-row
  encoders would scatter the fix across the codebase. ijson's
  `use_float` is the single point of control.
- **Async collector (`httpx.AsyncClient`).** The scheduler is
  blocking-thread (APScheduler `BlockingScheduler`); async here
  would either require a thread bridge or a scheduler rewrite. The
  HTTP call is ~3-9 seconds, the parse + insert is another few
  seconds, the cycle wall time is dominated by neither. Sync is
  enough.

## Consequences

- One new module (`collectors/pricempire.py`, ~400 LOC) with eleven
  unit tests covering the cents-to-dollars conversion, dedup gate,
  Decimal/float serialization, swap-gg wire-key normalization,
  Skinport placeholder handling, and the four failure modes.
- One new Python dependency: `ijson>=3.5.0`.
- The `PRICEMPIRE_API_KEY` env var is required by the collector
  service in `docker-compose.yml`. Missing key → fail-fast,
  zero-cost cycle.
- Phase 2b's drift detection will read from
  `pricempire_observations` alongside `prices`, with the
  `last_checked_at` field as the freshness gate (analogous to
  `observation_log.last_observed_at`).
