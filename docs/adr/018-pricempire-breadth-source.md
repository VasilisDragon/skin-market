# ADR 018 — Pricempire as breadth-coverage data source

**Status:** Accepted
**Date:** 2026-05-16
**Related:** ADR 009 (scheduler design, dedup-on-write), ADR 014 (read API), ADR 017 (timestamp split), Phase 2a (migrations 0005 + 0006)

## Context

Phase 0 confirmed there is no Pricempire integration in v1
(`PROJECT_OVERVIEW.md §0`). v1's curated 48-item watchlist is polled
end-to-end by per-source collectors against Steam Market, Skinport,
and DMarket — full control, high fidelity, but inherently
limited-scope. Phase 2's goal is breadth: serve price snapshots for
items outside the watchlist without giving up the curated-watchlist
fidelity.

Pre-Phase 2 diagnostic
(`docs/pre-phase2-pricempire-diagnostic.md`) characterized
Pricempire's `/v4/paid/items/prices` empirically. The relevant
findings for this decision:

- Single-shot bulk-full call pattern. One HTTP call returns the
  entire CS2 catalog (~39,400 items, ~33 MB at 3-source filter, ~64
  MB at 6-source filter). No pagination, no `app_id` filter (silent
  no-op), no per-item endpoint.
- Five real third-party providers (`buff163`, `skinport`, `dmarket`,
  `csmoney`, `swapgg`) plus one buy-side variant (`buff163_buy`).
  Pricempire's `sources=` filter is strict.
- **Pricempire does NOT serve Steam Market data via this endpoint** —
  passing `sources=steam` returns 200 but with 0 Steam price rows
  per item. The Steam collector remains the only Steam-pricing
  source for Phase 2a+.
- ~88% per-provider coverage on `buff163`/`skinport`/`dmarket` —
  exact-match 34,802 items out of 39,392.
- Wire prices are integer cents. Skinport rows carry a placeholder
  `updated_at = "2025-01-01T00:00:00.000Z"` while `last_checked_at`
  is real-time; we must surface both honestly.
- Developer-tier budget is 10,000 calls/month. Hourly polling costs
  720/month (7.2%); 15-min polling costs 2,880/month (29%). Even with
  inventory lookups (15% of budget), there is significant headroom.

## Decisions

### 1. Pricempire is a breadth layer on top of the existing collectors, not a replacement

The Steam/Skinport/DMarket collectors stay. They provide:

- **Steam coverage** (Pricempire serves zero Steam price rows).
- **Per-item polling fidelity** on the curated watchlist (Steam at
  60-min cadence; Skinport/DMarket at 15-min). Per-item failure
  handling, rate-limit backoff, the title-mismatch guard for DMarket.
- The `observation_log` poll-freshness signal (ADR 017) used by the
  bot's `🟡 stale` rendering, the deals freshness gate, and the
  unavailability-streak analytics.

Pricempire layers on top, providing:

- **Catalog breadth.** One call covers all 39k CS2 items, including
  items not in the curated watchlist. Phase 2b will expose this to
  the bot via an on-demand path; Phase 2a just gets the data
  flowing.
- **Cross-marketplace breadth.** Per-item rows from buff163, csmoney,
  swapgg, and a buff163-buy variant — markets we don't poll
  directly today.

The architecture diagram in `ARCHITECTURE.md` is extended to show
Pricempire as a fourth upstream feeding a separate hypertable rather
than a fourth per-item collector.

### 2. Separate `pricempire_observations` hypertable; do NOT reuse `prices`

`prices` is the home of per-item, per-source observations from the
curated polling collectors. Three considerations argue against
co-mingling Pricempire data into it:

- **Schema mismatch.** Pricempire returns three timestamps per
  observation (the local write time, Pricempire's
  `last_checked_at`, Pricempire's `updated_at`). `prices` carries
  one. Bolting two more columns onto `prices` to satisfy
  Pricempire's shape would corrupt the column semantics for the
  90%-by-source-count of rows that don't need them.
- **Cardinality blast.** Per scheduled cycle Pricempire writes up to
  39k items × 6 providers = 234k rows worst case. At v1 the curated
  collectors total ~17k rows/day. Mixing them in one hypertable
  would shift the chunk-size distribution and complicate
  TimescaleDB tuning. A separate hypertable keeps the curated
  table's compression and chunking decisions independent.
- **Provenance / honesty.** Pricempire is a *secondary*
  ingest — its rows reflect Pricempire's view of the market, not our
  direct poll. Phase 2b drift detection will explicitly compare
  Pricempire-derived rows to direct-poll rows; that comparison
  reads cleaner when the two are stored separately and the join
  intent is explicit.

Migration 0005 creates `pricempire_observations` with the same
composite PK shape `(item_id, source_id, timestamp)` as `prices` so
existing helpers (e.g. the dedup pattern from `collectors.base`)
transfer cleanly.

### 3. Six sub-provider source rows + one `pricempire` pseudo-source

Pricempire bundles six providers under one HTTP call. The sources
table has historically held one row per
independently-scheduled source (steam_market, skinport, dmarket). For
Pricempire we keep both shapes:

- **Six sub-provider rows** (`pricempire_buff163`,
  `pricempire_buff163_buy`, `pricempire_skinport`,
  `pricempire_dmarket`, `pricempire_csmoney`,
  `pricempire_swap_gg`). These exist so that:
  - Per-row `source_id` foreign keys in `pricempire_observations`
    point at meaningful provider rows.
  - Downstream queries that filter `WHERE s.enabled = TRUE` see
    Pricempire as live sources without any special-casing.
  - The Phase 2b drift logic can name "Pricempire's view of
    Skinport" as a separate first-class entity from "our direct
    Skinport poll" (`skinport`).
- **One `pricempire` pseudo-source row.** This row carries the
  schedule (`interval_minutes = 15`) and the `enabled` flag the
  scheduler reads. The scheduler iterates
  `sources WHERE enabled = TRUE` exactly as it always has; it
  special-cases `pricempire_*` rows (skips, since they're not
  independently scheduled) and bulk-snapshot pseudo-sources (a
  small `_PSEUDO_SOURCES` set in `collectors.scheduler`).

The alternative — hardcoding the Pricempire schedule in scheduler.py
instead of using a sources-table row — was rejected because it would
fork the scheduling model. Adding a hypothetical second
bulk-snapshot source later (e.g. Pricempire's `/inventory` cron)
would then need yet more hardcoding. The pseudo-source pattern
generalizes.

### 4. Three distinct timestamps in `pricempire_observations`

Phase 1 (ADR 017) taught the project to be precise about freshness
fields. Pricempire's payload carries two provider-asserted
timestamps per price row, neither of which is "when we wrote this":

- `timestamp` (local clock at row-write time) — drives the dedup
  gate, the TimescaleDB chunking, and is the project-canonical
  "when did we record this" field. The same role
  `prices.timestamp` plays for the curated collectors.
- `last_checked_at` (Pricempire's claim) — when Pricempire claims
  it polled the upstream provider. Phase 2b drift signal: if
  Pricempire's `last_checked_at` lags `timestamp` badly,
  Pricempire isn't actually refreshing.
- `updated_at` (Pricempire's claim) — when Pricempire thinks the
  underlying price actually moved. Informational. Skinport rows
  carry a placeholder `2025-01-01T00:00:00.000Z` here in practice;
  Phase 2b drift logic must tolerate this.

The migration's column comments document this so a future maintainer
reading the schema in psql sees the distinction inline.

### 5. Dedup gate matches the curated collectors

`(price, count)` parity with the latest existing row for the same
`(item_id, source_id)` → skip insert. Same shape as
`collectors.base.should_write_observation` (ADR 009 §3). No
`observation_log` analog yet — Phase 2b decides whether drift
detection needs one. Until then, dedup compares against
`pricempire_observations` itself.

### 6. Phase 2a only ingests Pricempire data for curated-watchlist items

The collector iterates the entire 39k-item response but persists
rows only for items already in our `items` table. Phase 2b will add
the long-tail layer (indexed items outside the watchlist). Keeping
Phase 2a scoped to curated items means:

- Phase 2a's storage growth is bounded by the watchlist size (~48
  items × 6 providers × 96 cycles/day ≈ 27k rows/day max, less
  after dedup). Phase 2b can decide retention / compression
  policies with empirical numbers in hand.
- Drift detection in Phase 2b launches with a known correspondence
  set: every Pricempire row has a curated counterpart for at least
  one of the three direct-polled sources.

## Rejected alternatives

- **Use Pricempire as the primary source for Skinport/DMarket; retire
  the direct collectors.** Rejected: gives up Phase 1's freshness
  fidelity (per-item, 15-min cadence, observation_log) and the
  per-source failure-handling instrumentation. Pricempire's Skinport
  row carries a placeholder `updated_at` and one-call-for-everything
  cadence; not a clean fit for the bot's stale/fresh rendering.
- **Co-mingle Pricempire rows into `prices` with a synthetic
  source_id.** Rejected for schema-shape and provenance reasons in §2
  above.
- **Hardcode the Pricempire schedule in `scheduler.py`** rather than
  use a `sources` row. Rejected: forks the scheduling model; the
  pseudo-source approach (§3) generalizes to future bulk-snapshot
  sources.
- **Run Pricempire at a finer cadence (5 min).** Possible within
  budget (86% of 10k/month) but no Phase 2a use case demands it.
  Deferred — 15 min keeps the budget at 29% with headroom for
  inventory lookups and growth.

## Consequences

- One new TimescaleDB hypertable (`pricempire_observations`) with the
  standard 7-day chunking. No compression policy in Phase 2a; revisit
  when the table's first hot chunk crosses ~100k rows or storage
  becomes a concern.
- Seven new rows in `sources`: six sub-providers + one pseudo-source.
  Existing `WHERE s.enabled = TRUE` queries continue to operate
  against `prices` only, so the new rows have zero behavioral effect
  on items.py / deals.py / the analytics layer until Phase 2b wires
  them in deliberately.
- One new env var on the collector service:
  `PRICEMPIRE_API_KEY`. The collector fails fast (logs ERROR,
  returns without HTTP call) when this is empty so a misconfigured
  deploy doesn't silently run zero-effective cycles.
- A new collector module (`collectors/pricempire.py`) lives outside
  the `BaseCollector` abstraction by design — see ADR 019.
- An updated `_PSEUDO_SOURCES` set in `collectors/scheduler.py`
  documents the bypass list; new bulk-snapshot sources in the future
  join here.
