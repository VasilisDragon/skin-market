# ADR 030 - Public-inventory portfolio baseline

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 014, ADR 016, ADR 028, ADR 029

## Context

The single-asset inventory and inspect-link tools are useful for one-off checks,
but a paid Discord bot needs a personal portfolio workflow. Current market tools
commonly advertise inventory analyzers, portfolio tracking, price alerts, and
top-item views. The lowest-risk version that fits the deterministic-core rule is
a public-inventory summary: read a public Steam inventory, match CS2 assets to
local market-name rows, and return a summed market baseline.

This is intentionally not real per-asset repricing. Float, seed, sticker, charm,
and pattern premiums remain surfaced as attributes only.

## Decision

Add:

```text
POST /asset-valuations/inventory/summary
```

with body:

```json
{"inventory_url": "https://steamcommunity.com/profiles/.../inventory/"}
```

The route accepts a plain profile inventory URL or an existing item URL with a
`#730_2_<asset_id>` fragment. The fragment is ignored for summary purposes after
validating that it points to CS2 app/context `730_2`.

The deterministic API path is:

1. Parse the inventory owner from `/profiles/<steamid64>/inventory/` or
   `/id/<vanity>/inventory/`.
2. Resolve vanity IDs through Steam Community XML using the existing resolver.
3. Fetch the public inventory through Pricempire's inventory endpoint.
4. For each asset with a `market_hash_name`, load latest local USD price points.
5. Build each item's market-name baseline.
6. Sum low/mid/high across priced assets and return top priced items, largest
   baseline-spread items, stickered count, concentration, and an unpriced
   sample.

The Discord bot gets a `market_baseline_inventory_summary` tool wrapper. The LLM
only routes and renders the structured result; it does not fetch, parse, or
compute totals.

## Consequences

- Users can paste one public inventory URL and get a quick portfolio baseline
  inside Discord.
- The response reports priced/unpriced counts, stickered count, top-item share,
  and largest baseline spreads so missing coverage and concentration risk are
  visible.
- The top-items sample keeps the tool result bounded for the LLM.
- Totals are market-name baselines. They are not a liquidation quote, trade
  offer recommendation, or sticker/float-aware appraisal.

## Rejected

- **Persist user portfolios now.** Rejected for this pass. Persistence enables
  alerts and P/L tracking, but it needs user identity, privacy policy, quotas,
  and schema work.
- **Include Steam Wallet credit in totals.** Rejected. The portfolio baseline is
  USD-only and follows ADR 014's denomination separation.
- **Fake premium-aware totals.** Rejected. Premium-aware portfolio valuation is
  Phase 3.5-style repricing and requires calibrated known-answer sales.
