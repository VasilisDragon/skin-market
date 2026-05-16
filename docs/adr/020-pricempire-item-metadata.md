# ADR 020 — Pricempire item-metadata extraction

**Status:** Accepted
**Date:** 2026-05-16
**Related:** ADR 018 (Pricempire as breadth-coverage data source), ADR 019 (Pricempire collector design), Phase 2a follow-up (migration 0007, `collectors/pricempire.py`)

## Context

Pricempire's `/v4/paid/items/prices` response carries two kinds of
data per item:

- **Per-provider price rows** (the nested `prices: [...]` array) —
  one record per sub-provider, fast-changing. Handled by ADR 019
  and stored in `pricempire_observations`.
- **Item-level metadata** — `rank`, `liquidity`, `marketcap`,
  `count`, `trades_7d` / `_30d` / `_90d`, `steam_last_7d` / `_30d` /
  `_90d`. These describe the *item*, not any individual price. They
  change slowly (rank shifts a few positions per day; marketcap
  drifts gradually; the trade-volume counters refresh once per day
  on Pricempire's side).

Phase 2a Step 2 (ADR 019) deliberately stored the whole wire item
dict in `pricempire_observations.raw_response` JSONB as a forward-
compat hedge. That decision is now coming due: the metadata fields
are useful inputs to Phase 2b drift detection and to the watchlist-
re-seed proposal, and JSONB-path queries are uncomfortable both to
read and to index.

This ADR captures the decision to lift those fields into typed
columns on a new `pricempire_item_metadata` hypertable, written as
a side effect of each price-ingest cycle.

## Decisions

### 1. Extract metadata as a side effect of the price cycle, not via a separate metas-cron

The wire response already carries the metadata fields, so the
collector that pulls prices has them in hand. Two alternatives
considered:

- **Separate `/v4/paid/items/metas` cron.** Pricempire has a metas
  endpoint that returns 91,294 items with richer fields (it also
  carries `steam_last_24h`, which `/prices` doesn't). A separate
  cron at a coarser cadence (e.g. daily) could pull that endpoint
  and populate the same table. Rejected for Phase 2a: introduces a
  second scheduled job, a second API-budget consumer, and a second
  failure mode for a feature that's currently a side benefit of
  the existing price cycle. The metas-cron is on the table for
  Phase 2b if drift detection demands the richer field set.
- **Keep the fields in `pricempire_observations.raw_response`
  JSONB.** Already working today. Rejected because JSONB-path
  reads scatter the access pattern across queries
  (`raw_response->>'rank'::int` everywhere); a typed column with a
  matching index is both faster and less error-prone.

The chosen pattern — extract at write time, dedup-gate against the
typed columns — gives us:

- Zero incremental API cost (we already make the call).
- Typed columns the rest of the codebase can query without JSONB
  acrobatics.
- A dedup gate that suppresses the steady-state no-op writes (most
  cycles).

### 2. Separate hypertable, NOT a column extension to `pricempire_observations`

Per-item metadata has different cardinality and refresh shape than
per-provider price rows:

- `pricempire_observations`: keyed `(item_id, source_id, timestamp)`,
  six rows per item per cycle (one per provider), fast-changing.
- `pricempire_item_metadata`: keyed `(item_id, timestamp)`, one row
  per item per *change* (not per cycle), slow-changing.

Bolting the item-level fields onto every `pricempire_observations`
row would duplicate them six times (once per provider) and trigger
dedup-gate misses every time a per-provider price changes but
metadata is stable. The separate table makes the cardinality honest
and lets the dedup gate operate at the right granularity.

The schema shape mirrors `pricempire_observations` for consistency
(composite PK with `timestamp`, hypertable, DESC index on
`(item_id, timestamp)`). Future maintainers reading both tables
side-by-side don't need to context-switch.

### 3. Defensive numeric-string parser (`_coerce_int`)

Pricempire's wire format is inconsistent across its two endpoints:

| Field | `/prices` form | `/metas` form |
|---|---|---|
| `rank` | `"554"` (numeric string) | `23219` (native int) |
| `marketcap` | `"215473980"` | `1345` |
| `count` | `"9060"` | `1345` |
| `trades_7d` | `"29"` or `null` | `null` |
| `liquidity` | `62.802508437142585` (native float) | `75` (native int) |

The collector reads `/prices` today, so the numeric-string form is
the common case. But the parser is tolerant of int, float, numeric
str, and `None` uniformly. Booleans are rejected (coerce to `None`)
to avoid silently masking a wire-format change to a boolean field.
Malformed values fall back to `None` rather than crashing the
cycle.

This generalizes for a future `/metas`-cron without code changes.

### 4. `steam_last_24h` column exists but stays NULL today

`steam_last_24h` is only present on `/v4/paid/items/metas`, not on
`/v4/paid/items/prices`. Phase 2a reads `/prices`, so the column
will always be NULL. Two options were considered:

- **Omit the column for now**, add it via a future migration when
  the metas-cron lands. Rejected: schema migrations have a real
  cost, and the column's typed shape is obvious from the sister
  columns. Adding it now and leaving it NULL has no runtime cost.
- **Include the column**, leave NULL, document the reservation in
  the migration. Chosen. The migration's column comment and ADR
  020 §1 both spell out why it's reserved.

### 5. Dedup gate on the full metadata tuple

The gate compares all 11 metadata fields (10 ints + `liquidity` as
a NUMERIC(6,2)) against the most recent existing row for that
item. Quantizing `liquidity` to two decimals on the wire side is
load-bearing: without it, a wire float of `62.802508437142585`
would never tuple-equal a stored `Decimal('62.80')` and we'd write
every cycle.

The gate suppresses writes when the tuple is identical, NULL
included. A missing field (Pricempire-side null) is treated as "no
change" rather than "changed to null", which matches operator
intent.

Empirical: first live cycle wrote 48 rows (one per item, expected,
since the table started empty). Second cycle is projected to write
~0-5 rows depending on which items had any field shift in the 15-
min window. ADR 019's per-cycle observation log line surfaces both
counters.

### 6. No analytics, no bot exposure, no drift logic in this phase

Following the brief: this is data-collection infrastructure only.
Phase 2b decides what to do with the data (drift detection,
ranking-based watchlist selection, etc.). Phase 2a's job ends with
"queryable typed columns are flowing."

## Rejected alternatives

- **Single mega-table with a `kind` discriminator column** mixing
  per-provider price rows and per-item metadata rows. Rejected:
  schemas-by-discriminator are a known anti-pattern; queries would
  always need a `WHERE kind = '...'` filter and the table's index
  story gets muddier.
- **Wide-format columns on `items`** (a column per metadata field,
  no time series). Rejected: items is a *registry* of curated
  watchlist members; mutating its rows on every collector cycle
  would confuse the table's role. Phase 2b drift detection also
  benefits from seeing how rank / liquidity *changed* over time,
  which a single-row-per-item shape can't express.
- **Write metadata as a separate row to `pricempire_observations`
  with a synthetic `source_id = 0` ("item-level")**. Rejected: a
  fake source row is a maintenance hazard; downstream code that
  filters by enabled sources or per-source semantics has to learn
  the synthetic-row carve-out. The separate table is honest.

## Consequences

- One new TimescaleDB hypertable, one new SQLAlchemy model
  (`PricempireItemMetadata`). No compression policy yet — revisit
  when storage warrants (projected ~1-5 rows per item per day in
  steady state, ~250-1250 rows/day total at 48 items).
- The Pricempire collector cycle now has two write paths per item:
  the per-provider price rows AND the per-item metadata row. Both
  share the per-item commit cadence (ADR 019 §8). A mid-cycle
  SIGKILL keeps the same partial-progress guarantee.
- A future `/v4/paid/items/metas` cron, if Phase 2b needs it, will
  write to the same `pricempire_item_metadata` table. The dedup
  gate naturally tolerates two writers on the same item if their
  metadata happens to agree; if they diverge, the latest write
  wins and the divergence shows up as drift in time-series queries.
- `steam_last_24h` is reserved for that future cron. The column is
  NULL on every Phase 2a-written row.
- Phase 2b inputs: rank + liquidity are now first-class queryable
  for the watchlist re-seed proposal
  (`docs/phase2b-watchlist-proposal.md`), without re-running the
  Phase 0 diagnostic samples.
