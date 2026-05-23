# ADR 036 - Portfolio baseline monitors

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 030, ADR 032, ADR 033

## Context

Portfolio snapshots let users ask how their public inventory baseline has moved,
but paid users should not need to remember to run the snapshot manually. A
portfolio monitor should periodically create a summary-level snapshot and notify
the configured Discord channel when the market-baseline movement is meaningful.

The monitor must keep the same honesty boundary as ADR 030 and ADR 032: this is
market-name baseline movement, not realized P/L and not float/sticker-aware
repricing.

## Decision

Add a `portfolio_monitors` table with:

- Discord user/channel ownership,
- public inventory URL,
- interval and change-threshold settings,
- optional quiet hours using a fixed UTC offset,
- delivery bookkeeping and last delivered snapshot id.

Expose deterministic API routes:

```text
POST /portfolio/monitors
GET  /portfolio/monitors
POST /portfolio/monitors/{id}/cancel
POST /portfolio/monitors/evaluate
POST /portfolio/monitors/{id}/delivery
```

Evaluation creates a fresh summary-level portfolio snapshot through the existing
snapshot path. The first snapshot is deliverable as an initial baseline. Later
snapshots are deliverable when `delta_vs_previous.mid_change_pct` exceeds the
configured absolute threshold. The Discord bot delivery loop sends deterministic
copy and records delivery state after send.

Portfolio monitor count is included in Discord entitlement quota policy.

## Consequences

- Users can subscribe an inventory URL and receive Discord updates only when the
  saved baseline moves enough.
- The LLM only creates/lists/cancels monitors. Snapshot creation, movement math,
  quiet-hour checks, and delivery state remain deterministic API concerns.
- Evaluation reuses the existing portfolio snapshot route, so it inherits
  snapshot quotas and the same no-premium limitation.

## Rejected

- **Persist full inventories for every monitor run.** Rejected. This pass stays
  with summary-level snapshots and bounded samples.
- **Treat movement as realized P/L.** Rejected. No sale occurred; the bot must
  describe this as market-baseline movement.
- **LLM-generated monitor notifications.** Rejected. Scheduled delivery should
  be cheap, deterministic, and independent of model availability.
