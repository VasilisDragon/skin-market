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
    LLM call,
  - delivery acknowledgement/retry state, optional quiet hours, and a
    configurable active-alert cap.
- Added persisted portfolio snapshots:
  - `POST /portfolio/snapshots`, `GET /portfolio/snapshots`, and
    `GET /portfolio/snapshots/trend`,
  - hidden Discord user ownership for save/list/trend tools,
  - latest-vs-previous and oldest-vs-latest movement calculations,
  - summary-level retention rather than full raw inventory storage.
- Added Discord entitlement/quota plumbing:
  - `GET /entitlements/discord/{discord_user_id}` and
    `PUT /entitlements/discord/{discord_user_id}`,
  - deterministic `free`, `trader`, and `pro` tier quotas,
  - API-side enforcement for active alerts and daily portfolio snapshots.
- Added a ranked market signal digest:
  - `GET /insights/signals/digest`,
  - `market_signal_digest` Discord tool,
  - deterministic severity/ranking/summary over recent spread and volume
    anomalies,
  - lane filters for broad, market-mover, and spread-watch digests.
- Added recurring signal digest subscriptions:
  - `POST /signals/subscriptions`, `GET /signals/subscriptions`,
    `POST /signals/subscriptions/{id}/cancel`,
    `POST /signals/subscriptions/evaluate`, and
    `POST /signals/subscriptions/{id}/delivery`,
  - channel delivery loop with deterministic digest messages,
  - quiet-hour support and entitlement-backed subscription quotas,
  - lane-specific channel subscriptions for broad, market-mover, and
    spread-watch feeds.
- Added recurring portfolio baseline monitors:
  - `POST /portfolio/monitors`, `GET /portfolio/monitors`,
    `POST /portfolio/monitors/{id}/cancel`,
    `POST /portfolio/monitors/evaluate`, and
    `POST /portfolio/monitors/{id}/delivery`,
  - scheduled summary-level snapshot creation,
  - thresholded Discord updates for market-baseline movement.

## Next features worth building

1. Alert variants and quiet hours.
   Alerts now have retryable delivery state, quiet hours, and
   entitlement-specific quota checks. The next paid-alert step is
   market-mover/drop variants beyond exact price thresholds.

2. Portfolio automation and item-level performance.
   Snapshot creation now supports scheduled baseline monitors. The paid product
   version should add retention controls and eventually full item-level
   performance after privacy/quota policy is explicit.

3. Real asset-specific repricing.
   Float, pattern, sticker, and charm premiums are the differentiator for
   serious traders. This needs known-answer sales and a calibrated premium
   model before it should affect user-facing dollar values.

4. Signal lanes / digest channels.
   The bot now has on-demand and scheduled lanes for broad, market-mover, and
   spread-watch digests. Future lanes such as liquid flips, sticker crafts, and
   float hunts need additional deterministic source data before they should be
   exposed as product promises.

5. Trade safety and execution context.
   Add trade-lock awareness, sold-status context when a source supports it, and
   safer "offer risk" explanations for shark/scam prevention.
