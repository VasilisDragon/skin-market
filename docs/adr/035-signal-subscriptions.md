# ADR 035 - Recurring signal digest subscriptions

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 031, ADR 033, ADR 034

## Context

ADR 034 added an on-demand market signal digest. That answers "what should I
watch right now?", but paid Discord products are most valuable when the right
signals arrive without manual polling. The existing alert delivery loop provides
the pattern: keep state in the API, let the bot deliver deterministic text, and
acknowledge delivery after Discord sends.

## Decision

Add a `signal_subscriptions` table with:

- Discord user/channel ownership,
- digest parameters (`hours`, `limit`, `threshold_z`),
- cadence (`interval_minutes`),
- optional quiet hours using a fixed UTC offset,
- delivery attempts, last delivery error, and last digest fingerprint.

Expose deterministic API routes:

```text
POST /signals/subscriptions
GET  /signals/subscriptions
POST /signals/subscriptions/{id}/cancel
POST /signals/subscriptions/evaluate
POST /signals/subscriptions/{id}/delivery
```

Evaluation selects active subscriptions that are due, outside quiet hours, and
have at least one qualifying signal. It returns the digest plus a fingerprint.
The bot delivery loop sends the digest to the stored channel and records
delivery state. Delivered fingerprints are not resent, so unchanged digests do
not repeatedly spam a channel.

Signal subscription count is part of the Discord entitlement quota policy.

## Consequences

- Paid users can subscribe a channel to recurring market-mover/spread-watch
  digests.
- The LLM only creates/lists/cancels subscriptions. It does not rank signals,
  decide due state, enforce quiet hours, or own delivery state.
- Quiet hours are deliberately simple: local hour plus fixed UTC offset. This
  avoids timezone database dependencies in the first pass.
- Delivery text is deterministic and does not spend LLM tokens.

## Rejected

- **Free-form cron expressions.** Rejected. Fixed interval minutes are easier to
  explain, validate, and bound by quotas.
- **LLM-generated digest messages.** Rejected. Delivery should remain cheap and
  predictable.
- **Full timezone names in v1.** Rejected. A fixed offset is enough to avoid
  overnight pings while keeping the data model simple.
