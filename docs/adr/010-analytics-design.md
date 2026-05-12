# ADR 010 — Analytics design: source-dynamic, divergence-first, SQL-native

**Status:** Accepted
**Date:** 2026-05-12
**Related:** docs/sources-and-semantics.md, ADR 006 (collector resilience), ADR 008 (Skinport collector)

## Context

Phase 5 introduces the analytics layer that turns raw `prices` rows
into per-item `insights`. Before writing it, several structural
decisions need to be pinned, mostly informed by the market-semantics
finding documented in `docs/sources-and-semantics.md`:

1. Steam Market and Skinport are not interchangeable price sources —
   they're priced in different *currencies* (wallet credit vs USD)
   with different *listing semantics* (single price vs across-variants
   minimum). Combining them into a single number is wrong.
2. The product roadmap commits to a third source (CSFloat in v2) for
   triangulation. Analytics must not be rewritten when that lands.
3. The 10 Steam "no parseable price" items per cycle are real rarity
   signal, not parse failures — anomaly detection must not flag them
   as anomalies, and the operational logs should ideally distinguish
   them from genuine collector errors.
4. Doppler / Marble Fade / Crimson Web items show 4×+ apparent moves
   on a single source within minutes. Filtering these as "implausible"
   would erase real market information.

## Decisions

### 1. Sources are dynamic; no hardcoded names in SQL

Every analytics SQL query iterates `sources WHERE enabled = TRUE`.
No string literal `'steam_market'` or `'skinport'` appears in
production code paths. Adding CSFloat (v2) or any future source is a
row in `sources` plus a collector module — no analytics rewrite.

Two practical implications baked into the code:

- The `volume_anomaly` SQL currently filters by
  `s.denomination = 'wallet_credit'` as a stand-in for "flow-style
  source." This is intentional: Steam's `volume` is a 24h flow,
  Skinport's `quantity` is current-listings stock; they don't
  compare. The proxy works today because Steam is the only
  wallet-credit source. A future RMB / wallet-credit source could
  break this — at that point we add an explicit
  `sources.observation_type` ('flow' / 'stock') column. A TODO in
  the code points at this.
- Cross-source spread iterates *every pair* of enabled sources
  (`itertools.combinations(...)`). With N sources we emit
  `N*(N-1)/2` spread rows per item per cycle. At N=2 (today) that's
  one row per item; at N=3 (CSFloat lands) it's three; at N=4, six.
  Still cheap.

### 2. Denomination tagging belongs in the schema

`sources` gets a `denomination` column (`'usd'` / `'wallet_credit'` /
future `'rmb'` etc.). The narrative job and the bot's reply formatter
both use it to render prices with context: "$28 USD on Skinport, $42
in Steam wallet credit." Averaging or stripping the denomination tag
is a bug.

Added in migration 0002 alongside `insights.text_value`.

### 3. Cross-source view + spread are first-class insight types

```
cross_source_view    — one row per item per cycle. value=NULL.
                       meta_info carries an array of per-source price
                       observations. Consumed by the bot to render the
                       "$28 USD / $42 wallet credit" view.

cross_source_spread  — one row per item per cycle PER PAIR of enabled
                       sources. value = (price_a - price_b) / price_b
                       (signed). meta_info carries both source names,
                       prices, and denominations.
```

The spread is a time-series in `insights` — anomaly detection runs
over its history (next decision).

### 4. Anomaly detection flags divergence, not single-source moves

A Doppler price 4× jumping on Skinport in five minutes while Steam
stays flat IS a `cross_source_divergence` anomaly. The same 4× with
Steam also moving is general market drift and NOT flagged. Per
`docs/sources-and-semantics.md`, single-source moves carry real
information that we don't want to filter or hide.

Implementation:

```
volume_anomaly             — per (item, source), latest 24h volume vs
                              7-day rolling mean of volumes; |z| ≥ 2
                              flags. Source-filtered to flow-style
                              denominations (Steam-only today).

cross_source_divergence    — per (item, source_a, source_b) pair, the
                              latest `cross_source_spread` vs the
                              7-day rolling mean of past spreads;
                              |z| ≥ 2 flags.
```

Z-threshold of 2.0 is the textbook default; if it produces too many
false-positives once the watchlist accumulates real history, we tune
in a follow-up. `MIN_VOLUME_SAMPLES` / `MIN_DIVERGENCE_SAMPLES` = 10
to prevent noisy stddevs from a near-empty baseline.

### 5. SQL window functions over continuous aggregates

At v1 scale — 50 items × 2 sources × ~hourly insights cycle ≈ a few
hundred rows in `insights` per hour, with the underlying `prices` table
growing at ~10–15k rows/day — the moving averages and anomaly
detection queries complete in single-digit milliseconds against the
composite PK index on `prices`. TimescaleDB continuous aggregates
would shave that to microseconds at the cost of one more piece of
machinery (CAGG definitions, refresh policy, the implicit "current
state may be stale by N minutes" gotcha). Not worth it yet.

If the watchlist grows past a few hundred items or we add high-cadence
sources (websocket pricing, etc.), continuous aggregates become the
right move. Today: hand-rolled CTEs + `AVG() OVER (...)` /
`STDDEV_POP()` are the simpler answer.

### 6. Steam "no listings" is a real signal, separate from parse failure

Per the Phase 4 review note: the collector currently lumps two cases
into one WARNING + one `unavailable` cycle counter:

- **No current listings on Steam** (success:true with no price fields,
  or success:false). This is the normal state for rare items most of
  the time. Frequency correlates with rarity/desirability.
- **Actual collector failure** (malformed body, parse error).

Phase 5 acknowledges the distinction in documentation but does NOT
add a new schema column or refactor the Phase 4 code. Reasons:

- Splitting at the collector level requires a per-cycle observability
  table (`collector_events` or similar) — non-trivial schema work
  for a signal we can mostly derive from the `prices` table's absence
  of recent rows.
- A subsequent ADR or v2 work item can add the table once we have
  real production data showing the cost of conflating these.

`docs/sources-and-semantics.md` and the narrative job's prompt both
treat "Steam unavailable" as real rarity information, not noise.

## Consequences

- **Pro:** the analytics is portable across source-set changes. CSFloat
  (v2) inserts a row in `sources`, the collector module ships,
  analytics works without modification on day one.
- **Pro:** the divergence-first anomaly model surfaces the events the
  product cares about (variant-mix shifts, wallet-credit liquidity
  changes, cross-marketplace arbitrage opportunities) without
  drowning in single-source noise.
- **Pro:** denomination tags propagate cleanly to the bot — the
  "$8143 vs $2121" example from the product brief is computed
  correctly by construction.
- **Con:** the `denomination = 'wallet_credit'` proxy for
  flow-vs-stock is fragile. When we add a third flow-style source,
  the SQL needs a real flag. TODO in code.
- **Con:** at very early stages of accumulation, the rolling-baseline
  approach to anomaly detection won't flag anything (insufficient
  samples). This is correct behavior but worth noting — operators
  shouldn't expect anomalies in the first day or two of running.
- **Related:** the bot's reply formatting (Phase 7) must read
  `cross_source_view` insights and render denominations honestly.
  The bot is not allowed to compute "current price" by averaging or
  picking one source.
