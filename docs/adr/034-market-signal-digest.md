# ADR 034 - Market signal digest

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 007, ADR 022, ADR 031, ADR 033

## Context

The bot already exposes raw anomaly rows through `whats_interesting`, but paid
users need a faster "what should I watch right now?" surface. A signal digest
should be concise, ranked, and Discord-native, without asking the LLM to score
or interpret raw insight rows.

## Decision

Add:

```text
GET /insights/signals/digest
```

The route reads existing deterministic insight rows from the last N hours:

- `cross_source_divergence`
- `volume_anomaly`

It ranks by absolute z-score, labels severity (`moderate`, `high`, `extreme`),
and returns a compact deterministic summary for each signal. The Discord bot gets
a `market_signal_digest` tool for "what should I watch", market-mover, spread
watch, and opportunity-priority requests.

## Consequences

- Users get a paid-style watchlist digest without paging through raw anomalies.
- The LLM renders ranked rows but does not calculate severity, rank, or summary
  text.
- The route reuses existing analytics outputs and does not add new collection
  cost.
- Signals are not trade instructions. They identify unusual watchlist behavior
  that may merit follow-up price, drift, or chart checks.

## Rejected

- **Generate digest text with the LLM.** Rejected. The first signal surface
  should be deterministic and cheap.
- **Create background channel subscriptions immediately.** Rejected for this
  pass. Subscription delivery needs quiet hours and channel policy; this route is
  the read-side primitive those subscriptions can use later.
