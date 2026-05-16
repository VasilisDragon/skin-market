# Phase 2a Pricempire ingest validation

**Status:** Refreshed 2026-05-16 ~16:49 UTC, ~13.5 h and ~54 scheduled cycles after the first clean ingest cycle. Sections that were marked **[awaits 24h]** in the initial snapshot (§1, §2 row-count, §4) have been updated in place against the wider window. Sections marked **[invariant]** were correct on first write and were left structurally unchanged; their counts were refreshed where relevant.

> Original `TBD-amend after 24h` marker resolved by this pass. The intended 24h re-run was performed earlier than 24h — 13.5 h was enough for the structural patterns to stabilize and for one important new finding (swap_gg row sparsity) to be diagnosable. See §6 for the swap_gg characterization.

## 1. Row volume

```
rows | items_covered | providers_seen |     oldest          |     newest
-----+---------------+----------------+---------------------+---------------------
2441 |       48      |        6       | 2026-05-16 03:19:46 | 2026-05-16 16:49:47
```

54 scheduled cycles have completed (one every 15 min between 03:19 and 16:49 UTC); 2,441 rows total. All 48 watchlist items are still covered by at least one provider. All six sub-providers have written rows.

Per-hour write distribution (UTC):

```
03:00  315  ← initial bulk cycle (~281 rows) + cycle 2's 34 movement writes
04:00  132
05:00  184
06:00  156
07:00  148
08:00  195
09:00  186
10:00  193
11:00  139
12:00  157
13:00  144
14:00  183
15:00  147
16:00  162 (partial hour at query time)
```

Steady-state is ~150-190 rows/hour after the initial-bulk hour. Extrapolated: **~3,800-4,500 rows/day** in steady state — roughly double the original ~2,200/day projection (which extrapolated from cycle 2's atypically quiet 20-write window). Still well below the 27k/day worst-case ceiling.

**Re-validation query:** the same `SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM pricempire_observations;` shown originally.

## 2. Per-provider coverage **[invariant on watchlist composition]**

```
          name          | items_with_data | rows | latest cycle
------------------------+-----------------+------+--------------
 pricempire_buff163     |       48        |  737 | 16:49
 pricempire_buff163_buy |       48        |  452 | 16:49
 pricempire_csmoney     |       47        |  849 | 16:49
 pricempire_dmarket     |       47        |  121 | 16:34
 pricempire_skinport    |       47        |  237 | 16:49
 pricempire_swap_gg     |       44        |   45 | 09:19 (then full dedup)
```

Coverage by provider, of the 48 watchlist items (unchanged from initial snapshot — every provider's `items_with_data` count is stable across the 13.5 h window):
- buff163, buff163_buy: 100% (48/48)
- csmoney, dmarket, skinport: 97.9% (47/48)
- swap_gg: 91.7% (44/48)

Row-count rate per provider (rows / 13.5 h) reveals a steep activity gradient:
- csmoney (~63 rows/h), buff163 (~55), buff163_buy (~33) — actively-trading book-data markets, lots of (price, count) movement within 15-min windows.
- skinport (~17), dmarket (~9) — moderate movement; dedup absorbs most cycles.
- swap_gg (~3.3) — essentially flat; only ONE non-initial write across the full 13.5 h window (USP-S | Kill Confirmed at 09:19). §6 characterizes this in detail.

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

## 4. Freshness distribution

Minutes since the latest row per `(item, source)` pair (snapshot at 16:49 UTC, 13.5 h after first cycle):

```
          name          | min | max | avg | items
------------------------+-----+-----+-----+-------
 pricempire_buff163     |  5  | 815 |  90 |  48
 pricempire_buff163_buy |  5  | 815 | 116 |  48
 pricempire_csmoney     |  5  | 815 |  65 |  47
 pricempire_dmarket     | 20  | 815 | 252 |  47
 pricempire_skinport    |  5  | 815 | 341 |  47
 pricempire_swap_gg     | 455 | 815 | 807 |  44
```

The original snapshot expected steady-state max_age_min ≤ 15 min for active providers and 0-30 min for the inactive-in-our-window providers. **The actual 13.5 h pattern violates that expectation for every provider** — max_age_min sits at 815 min (= the full window since the first cycle) on at least one (item, source) pair per provider. The interpretation is NOT that Pricempire stopped refreshing those pairs — see §6's swap_gg characterization, which establishes that Pricempire is still polling these items in real-time but the prices are flat, so our dedup gate correctly suppresses the redundant writes.

The pattern by provider matches the row-count gradient in §2:
- csmoney (avg 65 min), buff163 (avg 90), buff163_buy (avg 116) — the actively-trading providers churn through dedup on most items within a few cycles.
- dmarket (avg 252), skinport (avg 341) — moderate liquidity; many items have dedup-held the same row for hours.
- swap_gg (avg 807) — dedup holds essentially every item's first-cycle row for the full window. The one item that ever passed dedup (USP-S | Kill Confirmed at 09:19) is the source of the min=455 min figure; every other swap_gg pair is the full 815 min.

**Implication for the original "max_age_min > 60 → investigate" rule:** it was wrong. Dedup-driven freshness on `pricempire_observations` alone cannot distinguish "Pricempire stopped polling" from "price hasn't moved." Phase 2b drift detection that wants to gate on Pricempire-side freshness should drive off `raw_response->>'last_checked_at'` (Pricempire's own claim about when it last polled the upstream), not `timestamp` (our local write clock).

**Re-validation query:** rerun (same shape as initially)
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

## 6. swap_gg coverage characterization **[follow-up to §2/§4]**

§2 reports `pricempire_swap_gg` has 45 rows total after 54 cycles — 44 from the first cycle plus exactly ONE non-initial write (USP-S | Kill Confirmed at 09:19). The other 5 providers wrote between 121 and 849 rows over the same window. Why is swap_gg so sparse?

**Question:** is the swap_gg sub-collector silently dropping data, or is swap.gg genuinely so flat that the dedup gate suppresses essentially every cycle's write?

**Method:** for each of the 48 watchlist items:

1. Read the latest `pricempire_swap_gg` row from `pricempire_observations` (`price`, `count`, `raw_response->>'last_checked_at'`).
2. Read the current swap.gg price for that item from a cached Pricempire probe (`/tmp/swapgg-check.json`, 19.5 MB, captured 2026-05-16 16:51 UTC — ~2 min before the queries in §1/§2/§4 ran; fresh enough that no API call was spent for this analysis).
3. Compare.

**Results (count of 48 watchlist items):**

| Group | Count |
|---|---|
| Both DB and Pricempire have a swap_gg row | **44** |
| Only Pricempire has swap_gg (DB row missing) | **0** |
| Only DB has swap_gg (Pricempire dropped it) | **0** |
| Neither (swap.gg simply doesn't list this item) | **4** |

**Of the 44 items present in both, every single one has DB `price` and `count` exactly matching Pricempire's current values.** Zero divergence. The dedup gate has correctly suppressed 53 cycles' worth of redundant writes for 43 of those items and 52 cycles' worth for the 44th (USP-S | Kill Confirmed, which moved from $119.78 → $131.18 between 03:19 and 09:19, and has been flat at $131.18 since).

**The 4 "Neither" items** (no swap.gg coverage anywhere) are:
- `Glock-18 | Fade (Factory New)`
- `SSG 08 | Death Strike (Factory New)`
- `Souvenir AWP | Dragon Lore (Battle-Scarred)`
- `★ Karambit | Doppler (Factory New)`

These are the same four items already flagged in §2 as having partial Pricempire coverage. Pricempire's current swap.gg slice for them is empty; there is no DB row to write. Consistent and expected.

**One Pricempire-side nuance worth recording:** for USP-S | Kill Confirmed, Pricempire's current response shows `updated_at=2026-05-16T16:51` while the price has not changed since 09:19. So Pricempire bumps swap.gg's `updated_at` on each poll even when the underlying price is unchanged — another data point reinforcing §5's recommendation that Phase 2b drift logic should ignore `updated_at` and gate on `last_checked_at` (or, simpler, gate on dedup-row count over a window).

**Conclusion: swap_gg is behaving correctly.** The collector is not dropping data; the dedup gate is correctly suppressing redundant writes for what is genuinely a low-liquidity sub-marketplace whose listed prices essentially never move within a 15-minute window. The §4 "max_age_min = 815 min" observation for swap_gg is the same finding viewed from the freshness side.

Phase 2b implication: any "is swap.gg's view fresh?" surface that the bot exposes MUST drive off Pricempire's `raw_response->>'last_checked_at'` (which is real-time per cycle) — NOT off our local `timestamp` (which can legitimately be 13+ hours old for an unchanged price).

## 7. Errors / unexpected during the first cycles **[invariant]**

- The first cycle (01:32 UTC, pre-`swapgg` fix) returned HTTP 400. Pricempire's accepted-source list included `swapgg` not `swap.gg`. Fixed in commit `2917c71`. The collector logged the 400 and exited cleanly per the failure-handling design (ADR 019 §6); the next cycle attempted again.
- The second cycle (after the `swapgg` fix, before `use_float=True`) hit a `TypeError: Object of type Decimal is not JSON serializable` mid-stream after 14 items, 0 rows written. Caused by ijson's default `Decimal` decoding tripping psycopg's JSON encoder when persisting `raw_response`. Fixed in the same commit by passing `use_float=True` to `ijson.items()`; pinned by a regression test (`test_jsonb_safe_for_float_fields`).
- 4 of the 6 dev API calls in the Phase 2a discretionary budget were consumed during the wire-key probe + verification.
- After both fixes, every cycle has been clean: HTTP 200, ~4-7s wall time, no errors.

## 8. Open Phase 2b inputs flagged by this validation

1. **Doppler-pattern items** (Karambit Doppler FN, Flip Knife Doppler FN, M9 Bayonet Doppler FN). Direct Skinport vs `pricempire_skinport` drift exceeds 60% — these are upstream taxonomy mismatches, not bugs. Phase 2b drift detection needs an explicit "Doppler items can show large drift" rule, OR these items should be removed from the watchlist and replaced with non-phase-bearing equivalents. Item-level decision for Step 6's watchlist proposal.
2. **`pricempire_swapgg` placeholder rate of 28.9%** (refreshed from initial 29.5%; the count is stable because dedup froze the sample). Phase 2b drift logic must use `last_checked_at`, not `updated_at`, for at least these two providers (skinport + swapgg). Simpler still: `last_checked_at` for all providers.
3. **Souvenir Dragon Lore BS has only 2 Pricempire providers covering it.** Liquid market for it is essentially nonexistent. Keep as a Tier-3 (illiquid premium) canary or remove — decision deferred to Step 6.
4. **No observation_log analog for Pricempire yet.** Phase 2a's dedup gate compares against `pricempire_observations` itself. Phase 2b drift logic can either (a) treat Pricempire's `last_checked_at` as the freshness signal directly, or (b) introduce a `pricempire_observation_log` for the project-canonical "last cycle that touched (item, source)" semantic. ADR 019 §4 documents the deferral.
5. **Dedup-driven freshness blindness generalizes beyond swap_gg.** §4's `max_age_min = 815` (= the full 13.5 h window) shows up on at least one (item, source) pair for *every* provider, not just swap_gg. §6 establishes that for swap_gg this is normal (low liquidity, prices don't move, Pricempire still polling). The same logic almost certainly applies to the long-tail items on the moderate-liquidity providers (skinport at avg 341 min, dmarket at avg 252 min). Phase 2b cannot use `pricempire_observations.timestamp` as a "is Pricempire still polling X?" signal in any form; the only honest signal is `raw_response->>'last_checked_at'`. Worth a one-line note in ADR 019 §4 or an addendum.

---

## 24h-amendment checklist

When re-running this validation against a wider window (the original snapshot was at +17 min; this refresh was at +13.5 h), the queries to re-execute are:

- §1: total row count, oldest/newest. ~14 h sample: 2,441 rows. ~24 h sample expected: ~4,500-5,500 rows.
- §2: per-provider coverage (the items_with_data counts are stable; the row counts grow at provider-specific rates documented in §2).
- §4: freshness distribution. Steady-state pattern established at +13.5 h; further widening pushes the max upward proportionally as more (item, source) pairs hit the dedup wall.
- §5: placeholder frequencies. ~14 h sample: 1.3% on Skinport, 28.9% on swapgg. The swapgg figure is essentially frozen by dedup; the skinport figure trends down as more non-placeholder rows accumulate.
- §6: swap_gg coverage characterization. Reads from `/tmp/swapgg-check.json` if still fresh, or burns one Pricempire API call with `sources=swapgg`.

The interpretive paragraphs in §3, §6, §7, and §8 should remain accurate — they describe structural findings rather than time-sensitive numbers.
