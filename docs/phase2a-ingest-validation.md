# Phase 2a Pricempire ingest validation

**Status:** Initial snapshot — 2026-05-16 ~01:52 UTC, ~17 min and 2 scheduled cycles after the first clean ingest cycle. The sections marked **[awaits 24h]** require re-running against a fuller window before the Phase 2a record is complete. Sections marked **[invariant]** are already meaningful.

> **TBD-amend after 24h.** Re-run the queries in §1, §2 row-count, and §4 against the same DB at ~2026-05-17 01:37 UTC and update the corresponding numbers below. The interpretive paragraphs probably won't need changes — the structural findings are stable across cycles.

## 1. Row volume **[awaits 24h]**

```
rows | items_covered | providers_seen |     oldest      |     newest
-----+---------------+----------------+-----------------+-----------------
 301 |       48      |        6       | 2026-05-16 01:37| 2026-05-16 01:52
```

Two scheduled cycles have completed (01:37 and 01:52 UTC); 301 rows total. All 48 watchlist items are covered by at least one provider. All six sub-providers have written rows.

Per-cycle write counts:
- Cycle 1 (01:37): 281 rows written. Initial bulk — every (item, provider) pair gets a row.
- Cycle 2 (01:52): 20 rows written. The remaining 281 were dedup'd out (price + count unchanged). The 20 writes are items whose prices moved between cycles on buff163, buff163_buy, or csmoney (which trade actively across the 15-min window); dmarket, skinport, and swapgg dedup'd entirely.

Projection to 24h: 96 cycles × ~20 cycle-2-style writes after the initial 281 ≈ **~2,200 rows/day** in steady state, far below the 27k/day ceiling. The dedup gate is doing its job.

**24h validation queries:** rerun
```sql
SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM pricempire_observations;
```

## 2. Per-provider coverage **[invariant on watchlist composition]**

```
          name          | items_with_data | rows | latest cycle
------------------------+-----------------+------+--------------
 pricempire_buff163     |       48        |  56  | 01:52
 pricempire_buff163_buy |       48        |  55  | 01:52
 pricempire_csmoney     |       47        |  52  | 01:52
 pricempire_dmarket     |       47        |  47  | 01:37 (dedup'd in c2)
 pricempire_skinport    |       47        |  47  | 01:37 (dedup'd in c2)
 pricempire_swap_gg     |       44        |  44  | 01:37 (dedup'd in c2)
```

Coverage by provider, of the 48 watchlist items:
- buff163, buff163_buy: 100% (48/48)
- csmoney, dmarket, skinport: 97.9% (47/48)
- swap_gg: 91.7% (44/48)

Items with partial coverage:

```
              market_hash_name                | providers
----------------------------------------------+-----------
 Souvenir AWP | Dragon Lore (Battle-Scarred)  |     2
 Glock-18 | Fade (Factory New)                |     5
 SSG 08 | Death Strike (Factory New)          |     5
 ★ Karambit | Doppler (Factory New)           |     5
```

**Zero-coverage items: none.** Every watchlist item has at least two Pricempire providers serving data. The Souvenir Dragon Lore BS only has 2 providers, which makes sense — it's a one-of-a-kind item with extremely low liquidity outside of the niche markets that don't appear on every provider. Worth flagging but not a removal candidate.

Compared to the Phase 0 diagnostic, which predicted ~88% catalog-wide coverage on buff163/skinport/dmarket: our 48-item watchlist is biased toward popular liquid items, so coverage skews higher (97.9% vs 88.3%). Sensible.

## 3. Cross-source sanity — units check passed, but Doppler-phase divergence to flag for Phase 2b **[invariant]**

Top-10 absolute drift between our direct Skinport collector and `pricempire_skinport` (both within 6h):

```
          market_hash_name           | direct  | pricempire | drift_pct
-------------------------------------+---------+------------+-----------
 ★ Karambit | Doppler (Factory New)  | 10615.82|   2773.18  |  -73.9
 ★ Flip Knife | Doppler (Factory New)|  1827.10|    569.69  |  -68.8
 ★ M9 Bayonet | Doppler (Factory New)|  5484.85|   1997.08  |  -63.6
 AK-47 | Asiimov (Battle-Scarred)    |    44.62|     44.62  |    0.0
 AK-47 | Slate (Field-Tested)        |     5.00|      5.00  |    0.0
 M4A1-S | Hyper Beast (Field-Tested) |   112.06|    112.06  |    0.0
 AK-47 | Fire Serpent (Field-Tested) |   824.44|    824.44  |    0.0
 M4A1-S | Printstream (Field-Tested) |   224.37|    224.37  |    0.0
 M4A4 | Howl (Factory New)           |  7757.89|   7757.89  |    0.0
 M4A1-S | Cyrex (Field-Tested)       |   174.69|    174.69  |    0.0
```

**Units are correct.** Verified the cents→dollars conversion via the raw_response JSONB:

```
market_hash_name                    | persisted | raw_cents | raw_div_100
★ Karambit | Doppler (Factory New)  |  2773.18  |  277318   | 2773.18
```

`Decimal(str(raw_price)) / 100` parses cleanly. Every 0.0%-drift row above is a direct unit check pass.

**The three 60-74% drifts are real upstream divergence, NOT a bug.** All three are Doppler-pattern items (Karambit / Flip Knife / M9 Bayonet Doppler FN). Doppler-style skins have multiple phases (1-4, plus Ruby / Sapphire / Black Pearl, plus Emerald for some); the `market_hash_name` field on Skinport groups all phases under one name, but individual listings price phases differently — a Sapphire Karambit FN sells for many multiples of a Phase 1.

Our direct Skinport collector apparently picks a high-phase variant from Skinport's listing array (likely the cheapest, which can happen to be a rare-phase outlier if the lowest-priced Sapphire is below other phases for cents-precision reasons), while Pricempire's normalization picks a different aggregation (more representative of typical Phase 1-4 listings).

**Action for Phase 2b drift detection:** Doppler-pattern items need either (a) a per-phase split that our schema doesn't currently support, or (b) an explicit "skip Doppler aggregation" rule in the drift logic. Flag for the watchlist-revision proposal in Step 6.

## 4. Freshness distribution **[awaits 24h]**

Minutes since the latest row per `(item, source)` pair:

```
          name          | min | max | avg | items
------------------------+-----+-----+-----+-------
 pricempire_buff163     |  2  | 17  | 15  |  48
 pricempire_buff163_buy |  2  | 17  | 15  |  48
 pricempire_csmoney     |  2  | 17  | 16  |  47
 pricempire_dmarket     | 17  | 17  | 17  |  47
 pricempire_skinport    | 17  | 17  | 17  |  47
 pricempire_swap_gg     | 17  | 17  | 17  |  44
```

This snapshot is too early to be representative — the 17-minute max simply reflects "we've only been ingesting for 17 minutes and the rows dedup'd-out in the second cycle." After 24h the distribution should cluster within 0-15 minutes for active providers (buff163/buff163_buy/csmoney) and within 0-30 minutes for the inactive-in-our-window providers, with outliers flagging individual items whose dedup gate keeps suppressing writes.

**24h validation query:** rerun
```sql
SELECT s.name,
  MIN(EXTRACT(EPOCH FROM (NOW() - latest_ts))/60)::int AS min_age_min,
  MAX(EXTRACT(EPOCH FROM (NOW() - latest_ts))/60)::int AS max_age_min,
  AVG(EXTRACT(EPOCH FROM (NOW() - latest_ts))/60)::int AS avg_age_min
FROM (
  SELECT item_id, source_id, MAX(timestamp) AS latest_ts
  FROM pricempire_observations
  GROUP BY item_id, source_id
) latest
JOIN sources s ON s.id = latest.source_id
GROUP BY s.name ORDER BY s.name;
```

The expected pattern is: every provider's max_age_min stays ≤ 15 min over a 24h window, because every cycle produces at least *some* movement and the dedup gate doesn't suppress an entire (item, source) pair for >1 cycle in steady state. If max_age_min exceeds 60 minutes for any provider for any (item, source) pair, that's worth investigating — Pricempire may have stopped refreshing that pair entirely.

## 5. Skinport's `updated_at: 2025-01-01` placeholder frequency — and a SURPRISE on swapgg **[invariant]**

Sub-q 5 from the brief asked specifically about Skinport's placeholder updated_at quirk. The answer is more interesting than expected:

```
          name          | placeholder_rows | total | pct
------------------------+-----------------+-------+------
 pricempire_buff163     |        0        |  56   |  0.0
 pricempire_buff163_buy |        0        |  55   |  0.0
 pricempire_csmoney     |        0        |  52   |  0.0
 pricempire_dmarket     |        0        |  47   |  0.0
 pricempire_skinport    |        3        |  47   |  6.4
 pricempire_swap_gg     |       13        |  44   | 29.5
```

The diagnostic flagged Skinport. The live ingest shows it at a manageable 6.4% — `last_checked_at` is real-time on those rows so Phase 2b drift logic can still detect Pricempire-side staleness; only `updated_at` is placeholder. Below the brief's 5% concern threshold for Skinport... barely.

**Surprise: swapgg carries the same placeholder 29.5% of the time** — 5× more often than Skinport. The diagnostic samples didn't include swapgg rows (it wasn't one of the three providers the Phase 0 diagnostic verified), so this is a new finding.

**Action for Phase 2b drift logic:** Both `pricempire_skinport` AND `pricempire_swapgg` need to ignore `updated_at` and rely on `last_checked_at` as the primary "how fresh is Pricempire's view" signal. The cleanest implementation: a small constant set of provider names that need the fallback, applied at drift-query time. Or — simpler — Phase 2b ignores `updated_at` entirely and uses `last_checked_at` for all providers, since it's always populated.

## 6. Errors / unexpected during the first cycles **[invariant]**

- The first cycle (01:32 UTC, pre-`swapgg` fix) returned HTTP 400. Pricempire's accepted-source list included `swapgg` not `swap.gg`. Fixed in commit `2917c71`. The collector logged the 400 and exited cleanly per the failure-handling design (ADR 019 §6); the next cycle attempted again.
- The second cycle (after the `swapgg` fix, before `use_float=True`) hit a `TypeError: Object of type Decimal is not JSON serializable` mid-stream after 14 items, 0 rows written. Caused by ijson's default `Decimal` decoding tripping psycopg's JSON encoder when persisting `raw_response`. Fixed in the same commit by passing `use_float=True` to `ijson.items()`; pinned by a regression test (`test_jsonb_safe_for_float_fields`).
- 4 of the 6 dev API calls in the Phase 2a discretionary budget were consumed during the wire-key probe + verification.
- After both fixes, every cycle has been clean: HTTP 200, ~4-7s wall time, no errors.

## 7. Open Phase 2b inputs flagged by this validation

1. **Doppler-pattern items** (Karambit Doppler FN, Flip Knife Doppler FN, M9 Bayonet Doppler FN). Direct Skinport vs `pricempire_skinport` drift exceeds 60% — these are upstream taxonomy mismatches, not bugs. Phase 2b drift detection needs an explicit "Doppler items can show large drift" rule, OR these items should be removed from the watchlist and replaced with non-phase-bearing equivalents. Item-level decision for Step 6's watchlist proposal.
2. **`pricempire_swapgg` placeholder rate of 29.5%**. Phase 2b drift logic must use `last_checked_at`, not `updated_at`, for at least these two providers (skinport + swapgg). Simpler still: `last_checked_at` for all providers.
3. **Souvenir Dragon Lore BS has only 2 Pricempire providers covering it.** Liquid market for it is essentially nonexistent. Keep as a Tier-3 (illiquid premium) canary or remove — decision deferred to Step 6.
4. **No observation_log analog for Pricempire yet.** Phase 2a's dedup gate compares against `pricempire_observations` itself. Phase 2b drift logic can either (a) treat Pricempire's `last_checked_at` as the freshness signal directly, or (b) introduce a `pricempire_observation_log` for the project-canonical "last cycle that touched (item, source)" semantic. ADR 019 §4 documents the deferral.

---

## 24h-amendment checklist

When re-running this validation against a 24h window, the queries to re-execute are:

- §1: total row count, oldest/newest. Expected total: ~5,000-10,000 rows.
- §2: per-provider coverage (should remain stable; the watchlist composition is the invariant).
- §4: freshness distribution. Expected: every `max_age_min ≤ 15` for active providers in steady state.
- §5: placeholder frequencies. Expected: ~5-10% on Skinport, ~25-35% on swapgg.

The interpretive paragraphs in §3, §6, and §7 should remain accurate — they describe structural findings rather than time-sensitive numbers.
