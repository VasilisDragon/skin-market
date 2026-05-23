# ADR 032 - Persisted portfolio snapshots

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 030, ADR 031

## Context

ADR 030 added a stateless public-inventory portfolio baseline. That is useful
for one-off answers, but a paid Discord bot needs memory: users expect to ask
whether their inventory moved since the last check, what changed over time, and
whether a saved portfolio is trending up or down.

Full inventory persistence would retain every asset from a user's Steam
inventory. That is more sensitive than needed for the first paid-value pass and
would force privacy and retention decisions before the product has proved the
workflow.

## Decision

Add a summary-level `portfolio_snapshots` table owned by Discord user id. A
snapshot stores:

- SteamID64 and source inventory URL,
- status/message from the inventory baseline builder,
- low/mid/high USD market baseline totals,
- priced/unpriced/stickered counts,
- top-item share,
- bounded top-item, spread, and unpriced samples.

Expose deterministic API routes:

```text
POST /portfolio/snapshots
GET  /portfolio/snapshots
GET  /portfolio/snapshots/trend
POST /portfolio/snapshots/prune
```

The create route reuses the existing public-inventory baseline path, persists a
snapshot only when the inventory can be read, and returns movement versus the
previous snapshot for the same Discord user and Steam account. The trend route
returns latest, previous, latest-vs-previous delta, oldest-vs-latest delta, and
the bounded recent snapshot list.

The prune route previews or deletes old summary snapshots for a Discord user. It
can keep the latest N snapshots, optionally scope to one SteamID64, and
optionally delete only rows older than a configured age. The default is a
`dry_run` preview so the bot can show users exactly what would be removed before
destructive cleanup.

The Discord bot gets four LLM-callable tools:

- `save_portfolio_snapshot`
- `list_portfolio_snapshots`
- `portfolio_snapshot_trend`
- `prune_portfolio_snapshots`

Discord user context is injected by the bot. The LLM does not supply ownership
ids and does not calculate portfolio movement.

## Consequences

- Users can build portfolio history from Discord without repeating the same
  manual comparison.
- The first version is privacy-conservative: it persists summary baselines and
  bounded samples, not full raw inventories.
- Users can preview or prune their saved summary history without operator
  database access.
- The bot can answer trend/P&L-style questions from saved snapshots while
  stating that this is market-baseline movement, not realized P/L.
- Snapshot accuracy inherits ADR 030's limitation: totals are market-name
  baselines and do not include float, pattern, sticker, or charm premiums.

## Rejected

- **Persist full inventories immediately.** Rejected for this pass. Full
  inventory persistence is valuable later for item-level performance, but it
  should come with explicit retention and quota policy.
- **Let the model compare snapshots.** Rejected. Movement calculations belong
  in the API so Discord responses stay deterministic.
- **Treat market-baseline movement as realized P/L.** Rejected. A portfolio can
  move without a sale; the bot must not imply realized gains or liquidation
  certainty.
