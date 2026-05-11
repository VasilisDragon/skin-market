# ADR 008 — Skinport collector: bulk fetch, filter in Python, mapping

**Status:** Accepted
**Date:** 2026-05-11
**Related:** ADR 006 (collector resilience strategy)

## Context

Skinport's only programmatic endpoint for current prices is
`GET /v1/items?app_id=730&currency=USD`, which returns a JSON array
covering all ~6000 CS2 items in one response. There is no per-item
filter parameter. Our watchlist is 48 items.

The Steam collector built in Phase 2 sits on a per-item endpoint —
`Collector.collect_one(name)` is its natural unit. Skinport's natural
unit is the whole cycle. The base abstraction needs to accommodate
both.

## Decisions

### 1. Bulk fetch + Python-side filter

One HTTP call per cycle (5 minutes per the spec; Skinport tolerates
~8 req/s so we are far under the limit). The full response is parsed,
built into a `{market_hash_name: entry}` map keyed on NFC-normalized
names, and then we iterate the watchlist to look up matches. Items
not on the watchlist are discarded.

Alternative rejected: persist all ~6000 entries to a buffer table per
cycle, then promote watchlist matches downstream. Adds a table, adds
a stage, doubles write volume — and we have no concrete v1 use case
for the discarded entries.

### 2. Field mapping to `prices`

| `prices` column | Skinport field | Notes |
|---|---|---|
| `price`        | `min_price`  | Lowest currently-listed price. Semantically comparable to Steam's `lowest_price`. |
| `volume`       | `quantity`   | **Number of currently listed items**, NOT 24h sales like Steam's `volume`. See "Caveat" below. |
| `currency`     | `"USD"`      | Locked by query param. |
| `raw_response` | per-item dict | The slice for this item only — not the full ~6000-item dump. |

**Caveat about `volume`:** Steam's `volume` is a flow measurement
(units sold in the last 24 hours). Skinport's `quantity` is a stock
measurement (current open listings). They are not the same and
should not be averaged together. The analytics layer (Phase 5) will
keep them separate by querying per-source. The field name in `prices`
is the lowest-common-denominator "volume" because schema-renaming for
v1 would churn for thin benefit, but the meaning is documented here
and in code comments.

When `min_price` is null (almost always because `quantity == 0`), we
skip-and-log per ADR 006 — no NULL price rows.

### 3. Cycle-level timestamp

All observations from one Skinport cycle carry the same UTC timestamp
(the moment we received the response). Rationale: the response is a
server-side snapshot, not a per-item reading. Steam, by contrast,
timestamps per request because each call gets a fresh datapoint.
The composite PK `(item_id, source_id, timestamp)` keeps the rows
distinct across items, so the shared timestamp does not collide.

### 4. Base `Collector` abstraction tweak

`Collector` gains a `collect_cycle(client, market_hash_names) ->
Iterator[PriceObservation | None]` method. The default implementation
loops over `collect_one` with `inter_request_delay` between successive
calls — Steam uses this default. `SkinportCollector` overrides
`collect_cycle` for the bulk path; its `collect_one` is a trivial
wrapper that calls `collect_cycle([name])` and returns the first
result (kept for parity with the ABC and for the `--item`-style
debug path).

`persist_observation` moves to `collectors/base.py` so both
collectors share one implementation.

## Consequences

- **Pro:** one HTTP call per cycle is much friendlier to Skinport
  than per-item polling would be, and it's the only mode their API
  supports anyway.
- **Pro:** the watchlist filter keeps the prices table from inheriting
  6000 rows of items we'll never query.
- **Pro:** the `collect_cycle` abstraction is the right unit for the
  scheduler (Phase 4) to drive — one method, both collectors.
- **Con:** `Skinport.collect_one` is wasteful (it pulls the full
  6000-item response to filter to one). Only used for debug; the
  scheduler will use `collect_cycle`.
- **Con:** `volume` is overloaded between sources. Documented in the
  table above; analytics code must filter by source when comparing
  values. Worth a schema rename in v2 if it bites us.
- **Related** (v2 hardening): if cold-replay of historical bulk
  responses ever becomes useful, introduce a `raw_dumps` table that
  stores the whole response keyed on `(source, fetched_at)`. Not
  needed for v1.
