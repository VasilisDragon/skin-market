# Paid Discord bot feature gap

**Date:** 2026-05-23

## Research snapshot

Current CS2 market tools cluster around four paid-value themes:

- Portfolio/inventory analytics: inventory value, P/L, top holdings, item
  performance, export/reporting.
- Alerts and signals: price targets, price drops, market movers, bid-ask
  spreads, route/discount context, sold-status context.
- Asset-specific edge: float gems, sticker crafts, charms, pattern/phase
  detection, trade-lock context.
- Discord-native speed: concise commands, channel-specific signal lanes,
  reduced tab-hopping, and fast route validation inside Discord.

References checked:

- Pricempire Tools & Apps: inventory analyzer, portfolio tracker, deals finder,
  chart compare, advanced tools: <https://pricempire.com/app>
- Pricempire plans: free Trader plan includes alerts, favorites, portfolios, and
  inventory manager; API tiers unlock higher data access:
  <https://pricempire.com/subscribe>
- SteamLedger: portfolio tracking, P/L, price alerts, float/pattern data,
  trade-lock tracking, exports: <https://steamledger.com/>
- ArbitraCS: Discord-native signal lanes, liquid flips, float gems, sticker
  crafts, charm hunts, bid-ask spread, market movers, sold tracker context:
  <https://www.arbitracs.io/>

## Implemented in this pass

- Corrected exact-asset output from "valuation/value gauge" to
  `market_baseline`, with explicit no-premium wording.
- Added live, network-gated cross-checks for inventory and inspect fixtures.
- Added `market_baseline_inventory_summary`, a Discord tool and API route that
  returns a public inventory portfolio baseline with:
  - summed low/mid/high USD baseline,
  - priced/unpriced counts,
  - stickered item count,
  - top-item concentration,
  - top priced items,
  - largest baseline-spread items.
- Added optional DeepSeek rolling budget guards:
  - `DEEPSEEK_DAILY_COST_LIMIT_USD`,
  - `DEEPSEEK_DAILY_USER_COST_LIMIT_USD`.
- Added persistent Discord price alerts:
  - `POST /alerts/price`, `GET /alerts/price`,
    `POST /alerts/price/{alert_id}/cancel`, and
    `POST /alerts/price/evaluate`,
  - hidden Discord user/channel context injection for create/list/cancel tools,
  - an API-side deterministic threshold evaluator,
  - a background Discord delivery loop that sends triggered alerts without an
    LLM call.

## Next features worth building

1. Portfolio snapshots over time.
   The summary tool is stateless. Paid users will eventually expect daily
   inventory snapshots, P/L, item performance, and value-change alerts. This
   needs privacy/retention decisions before storing user inventories.

2. Alert hardening and quotas.
   The first persistent-alert pass lacks a delivery outbox and per-user quotas.
   Production paid tiers should add retry-safe delivery state, quota/entitlement
   checks, quiet hours, and market-mover/drop alert variants.

3. Real asset-specific repricing.
   Float, pattern, sticker, and charm premiums are the differentiator for
   serious traders. This needs known-answer sales and a calibrated premium
   model before it should affect user-facing dollar values.

4. Signal lanes / digest channels.
   The bot already has anomaly and drift primitives. The paid product version
   should turn those into concise "liquid flips", "market movers", and
   "spread watch" digests with configurable thresholds and quiet hours.

5. Trade safety and execution context.
   Add trade-lock awareness, sold-status context when a source supports it, and
   safer "offer risk" explanations for shark/scam prevention.
