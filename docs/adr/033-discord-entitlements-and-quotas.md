# ADR 033 - Discord entitlements and quotas

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 027, ADR 031, ADR 032

## Context

Persistent alerts and portfolio snapshots create background work and retained
state. A paid Discord bot needs a deterministic way to apply user-level limits
before live billing exists. The repository must not create payment accounts or
wire real payment processors autonomously, but it can model internal tier/quota
state that a future billing integration updates.

## Decision

Add an operator-managed `discord_entitlements` table keyed by Discord user id.
Rows store:

- `tier`: `free`, `trader`, or `pro`
- `status`: `active` or `disabled`
- created/updated timestamps

Expose protected API routes:

```text
GET /entitlements/discord/{discord_user_id}
PUT /entitlements/discord/{discord_user_id}
```

If no row exists, the API returns a `default` effective tier based on environment
fallbacks. Stored active rows use deterministic tier quotas:

| Tier | Active price alerts | Portfolio snapshots/day |
| ---- | ------------------- | ----------------------- |
| free | 3 | 3 |
| trader | 25 | 20 |
| pro | 100 | 100 |

Disabled rows receive zero quota. Price-alert creation and portfolio snapshot
creation read this policy API-side; the LLM does not decide quota eligibility.

## Consequences

- The bot now has billing-agnostic paid-tier plumbing without touching payment
  processors.
- Operators can grant, downgrade, or disable Discord users through the protected
  API.
- Quota checks run before expensive work where possible, preventing one user
  from creating unbounded alert or snapshot load.
- Existing single-operator deployments still work with environment fallback
  limits when no entitlement row exists.

## Rejected

- **Integrate live billing now.** Rejected. Account creation, payment keys, and
  payment-processor workflows are operator-owned actions.
- **Let the LLM enforce tiers.** Rejected. Tier state and quota checks are
  deterministic API concerns.
- **Store personal billing details.** Rejected. The table only stores Discord
  ids, tier, status, and timestamps.
