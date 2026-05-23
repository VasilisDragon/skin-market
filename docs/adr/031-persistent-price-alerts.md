# ADR 031 - Persistent Discord price alerts

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 014, ADR 016, ADR 027, ADR 030

## Context

Price alerts are a core paid-bot expectation: users want to ask for a target
once and receive the notification later without polling the bot manually. The
existing Discord bot already routes tool calls through a deterministic API, but
it did not persist user intent or deliver background notifications.

This feature must preserve the deterministic-core rule. The LLM may interpret a
request like "alert me when AK Redline FT drops below $25", but it must not own
alert state, price math, threshold evaluation, or Discord delivery.

## Decision

Add a `price_alerts` table keyed by Discord user and channel context. Each row
stores:

- the item id plus slug/display-name snapshots,
- the denomination (`usd` or `wallet_credit`),
- alert mode (`price_threshold` or `percent_move`),
- the direction (`at_or_below` or `at_or_above`),
- the stored absolute threshold,
- optional percent-move baseline price/source and requested percent,
- lifecycle state (`active`, `triggered`, `cancelled`),
- the latest evaluation and trigger metadata,
- delivery acknowledgement and retry metadata.
- optional quiet-hour delivery window using a fixed UTC offset.

Expose deterministic API routes:

```text
POST /alerts/price
GET  /alerts/price
POST /alerts/price/{alert_id}/cancel
POST /alerts/price/evaluate
POST /alerts/price/{alert_id}/delivery
```

The Discord bot gets three LLM-callable tools for create/list/cancel. Discord
user and channel ids are injected by `bot.deepseek_client`; the model does not
see or supply that ownership context. A separate bot background loop calls the
evaluation endpoint, formats triggered alerts with deterministic copy, and sends
them to the stored channel. No DeepSeek call is used for delivery.

Triggered alerts are not considered delivered until the bot records a successful
delivery acknowledgement. Failed sends increment `delivery_attempts` and keep the
row eligible for retry until `PRICE_ALERT_MAX_DELIVERY_ATTEMPTS` is reached.
Creation is capped by `PRICE_ALERT_MAX_ACTIVE_PER_USER` to provide the first
quota boundary for a paid tier.
If quiet hours are configured, triggered alerts remain pending until the user's
local quiet window ends.

Alert evaluation stays API-side:

1. Load active alerts in creation order up to a batch limit.
2. Resolve the latest local price points for the alert denomination.
3. Use the lowest current price for `at_or_below` and the highest current price
   for `at_or_above`.
4. Compare against the stored absolute threshold.
5. Mark matched rows as `triggered` with trigger price/source metadata.
6. Return triggered rows that still need Discord delivery.
7. Suppress pending-delivery rows while their quiet-hour window is active.

## Consequences

- Users can create durable price targets from Discord without needing to repeat
  the query.
- Alert ownership is bound to the hidden Discord user id, so users can only list
  and cancel their own alerts through bot tools.
- Delivery does not spend LLM tokens and does not depend on the model being
  available.
- Failed Discord sends are retryable instead of disappearing after threshold
  evaluation.
- Overnight notifications can be deferred without cancelling or losing the
  triggered alert.
- Percent-move alerts are reduced to an absolute trigger price at creation time,
  using the current deterministic source price as the baseline. Evaluation stays
  as cheap and deterministic as fixed-threshold alerts.
- The active-alert cap prevents one user from creating unbounded background work.
- The first version intentionally watches current market-name prices. It does
  not account for float, pattern, sticker, charm, trade-lock, or liquidity
  adjustments.

## Known limitations

- Quotas are a simple global per-user cap, not subscription-tier-aware yet.
- Alerts that exhaust delivery attempts stay triggered but undelivered. A future
  operator-facing repair surface should expose those rows.
- Alerts are item-slug based. Watchlist coverage still determines which items
  can be monitored.

## Rejected

- **Let the LLM decide whether an alert fired.** Rejected. The model may route
  the user's request, but threshold evaluation must remain deterministic.
- **Use a free-form item name instead of a slug.** Rejected for now. Slug-based
  creation avoids ambiguous item matching and matches the existing bot tools.
- **Send alerts through a model-generated response.** Rejected. Background
  delivery should be cheap, predictable, and independent of DeepSeek uptime.
