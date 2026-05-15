# ADR 017 — Split `observed_at` into `last_polled_at` and `last_changed_at`

**Status:** Accepted
**Date:** 2026-05-15
**Related:** ADR 009 (scheduler design, dedup-on-write), ADR 014 (read API), ADR 016 (Discord bot runtime), Phase 7a (`observation_log` migration 0004)

## Context

`/items/{slug}/price` (`api/routes/items.py`) returned a single `observed_at` field per source, sourced from `MAX(prices.timestamp)` for that `(item, source)` pair. The bot interpreted `observed_at` as "how recently we polled this" and prefixed any reading older than `STALE_HOURS=4` with 🟡.

That interpretation was wrong. `prices` is dedup-on-write (ADR 009 §3) — an unchanged poll does not insert a new row. So `prices.timestamp` is "the last time `(price, volume)` actually changed," not "the last time we polled." A flat market hour leaves `prices.timestamp` static even while the collector is polling cleanly every 15 minutes.

Phase 7a added `observation_log(item_id, source_id, last_observed_at)` precisely so the unavailability-streak analytics could distinguish "we polled and got nothing" from "we never polled." It is upserted unconditionally on every successful poll, pre-dedup. The API just wasn't reading it.

Empirical evidence at audit time (2026-05-15): 25 of 48 Skinport items showed `MAX(prices.timestamp) > 4h` even though Skinport polled every 15 minutes with zero failures — pure dedup-vs-display confusion. See `PROJECT_OVERVIEW.md §5` for the full numbers.

## Decisions

### 1. Surface two distinct timestamps in `PerSourcePrice`

- `last_polled_at: datetime` — from `observation_log.last_observed_at`. The freshness signal. Required.
- `last_changed_at: datetime | None` — from `prices.timestamp`. Informational only. Nullable defensively (in practice always set when the row appears).

`observed_at` was removed cleanly rather than kept as a deprecated alias. There are no external API consumers — only the in-compose bot — and a transient deprecated alias would just be drift waiting to happen.

### 2. Drive the `/items/{slug}/price` query off `observation_log`, not `prices`

```sql
FROM observation_log ol
JOIN sources s ON s.id = ol.source_id
JOIN LATERAL (
    SELECT timestamp, price, volume
    FROM prices
    WHERE item_id = ol.item_id AND source_id = ol.source_id
    ORDER BY timestamp DESC
    LIMIT 1
) p ON TRUE
WHERE ol.item_id = :item_id AND s.enabled = TRUE
```

The driving table is `observation_log`. Sources with no observation_log row are omitted from the response, even if a stale `prices` row exists for them. This is intentional: a `(item, source)` pair where the collector has never produced a recent successful observation is *not* fresh, and pretending the stale `prices` row represents one would re-create the original bug.

Items affected by this rule today are DMarket title-mismatch casualties (the `★ Moto Gloves | Spearmint (Field-Tested)` case in `PROJECT_OVERVIEW.md §5`). The bot fills the slot via its `never_observed` branch, which is now the honest answer.

### 3. Bot uses `last_polled_at` for the `state` decision; `last_changed_at` is informational

`bot/tools.py`'s `query_current_price` composer:

- Computes `minutes_since_polled` from `last_polled_at`.
- Decides `state = "stale" if minutes_since_polled > STALE_HOURS*60 else "fresh"`.
- When `last_changed_at` is meaningfully older than `last_polled_at` (gap ≥ 60 minutes), surfaces `price_flat_minutes` as an extra field — an informational hint, not a warning.

The system prompt was updated to spell out that `price_flat_minutes` is normal market behavior and MUST NOT be rendered with the 🟡 prefix.

### 4. Out-of-scope follow-up: `/deals/evaluate` has the same conceptual bug

`api/routes/deals.py` filters comparable freshness against `prices.timestamp`. It should also switch to `observation_log.last_observed_at`. Not fixed in Phase 1 — keeping the diff reviewable was higher-leverage than completeness. Tracked in `PROJECT_OVERVIEW.md §5`'s resolved-note.

## Rejected alternatives

- **Keep `observed_at` as a defensive alias for one release.** No external consumers — only the bot, in-compose, on the same deploy cadence — so the only effect would be code paying for backwards-compatibility it didn't need.
- **`last_polled_at: datetime | None` and surface stale-`prices`-without-`observation_log` rows.** Considered briefly to preserve the Moto Gloves case in the response. Rejected: a fresh `prices` row with no `observation_log` row would render with `last_polled_at=None`, and the bot would need a new "polling_stopped" state to render it correctly. Letting it fall through to `never_observed` is simpler and honest.
- **Compute `last_changed_at` lazily client-side.** The bot has the slug; it could query history and read the most recent timestamp. Two HTTP round-trips for a piece of info the API already has. No.

## Consequences

- API contract is breaking for any consumer reading `observed_at`. There are none outside this repo (`grep -rn observed_at` returns only the in-compose bot, which moves on the same deploy).
- Schema test (`test_schema_rejects_missing_last_polled_at`) guards against accidental drift.
- `test_polled_fresh_but_price_flat` exercises the exact divergence the bug exhibited.
- `test_source_without_observation_log_omitted` pins the Moto-Gloves-on-DMarket behavior.
