# Phase 2b live-cycle validation

**Window:** 2026-05-16 23:16 UTC → 2026-05-17 20:46 UTC (21h 30min, 44 drift cycles).
**Deploy:** `DRIFT_DETECTION_ENABLED=true` flipped at 2026-05-16 23:16 UTC (the first `drift_verdict` row's `computed_at` is the authoritative deploy marker; the 8c037cb commit's `Sat May 16 18:20:33 -0500` timestamp is commit-creation time, not deploy time).
**Coverage:** 42 deep-tier items × 2 meaningful pairs = 84 rows/cycle, 44 cycles, 3,696 total `drift_verdict` rows. No misfires.

This document characterizes the detector's observed behavior over its first day live. **It is not a validation against a quantitative pre-deploy prediction** — no Step 5 prediction artifact exists (Phase 2b's foundation shipped as a single mega-commit with no separate Steps 1-6 design docs; the prompt's "Step 5's predicted distribution" turned out to be Step 7.2's *observed* first-cycle snapshot, which is not a falsifiable prediction). §3 reframes accordingly.

Intended consumers: ADR 022's empirical-findings section; ADR 024's tier-awareness scope; TODO triage entries for the gaps surfaced in §4.5 and §7.

---

## §1. Cycle cadence and row volume

```sql
SELECT min(computed_at), max(computed_at), count(*),
       count(DISTINCT date_trunc('minute', computed_at)) AS distinct_cycles
FROM insights
WHERE insight_type = 'drift_verdict';
```

| min | max | total rows | distinct cycles |
|---|---|---|---|
| 2026-05-16 23:16:32 UTC | 2026-05-17 20:46:32 UTC | 3696 | 44 |

3696 = 44 × 84 exactly. Every cycle wrote the full 84 rows (42 items × 2 pairs); no truncations, no DB errors. Every cycle fired on the `:16` or `:46` minute, drift between cycles ≤ ~50ms. The 30-minute APScheduler interval is operating cleanly.

The first four cycles immediately after deploy (23:16, 23:46, 00:16, 00:46) carry "warmup" in the sense that some `pricempire_observation_log` rows were freshly backfilled from `raw_response.last_checked_at` (migration 0008 ran on deploy) and the curated `observation_log` had only minutes of post-deploy entries for some items. But — as §3 spells out — the *verdict distribution* in those cycles is essentially the same as later cycles, because the dominant shape is a structural bimodal oscillation that locked in from cycle 1. There is no monotonic warmup-to-steady transition to model.

---

## §2. Verdict distribution

### §2.a Primary: latest-cycle snapshot (bimodal per cycle — both phases shown)

`DISTINCT ON (item_id, source_a_id, source_b_id) ORDER BY ... computed_at DESC` over the full table yields one row per `(item, pair)` at its latest verdict. **Which cycle is "latest" matters** because the distribution bimodally alternates between two stable shapes (§3 and §3.a). Both phases shown here, sampled at adjacent cycles ~30 min apart:

**Phase A — "high stale" (cycle ending :16)** — sampled at 2026-05-17 20:16:32 UTC:

| verdict | n | distinct items |
|---|---|---|
| `stale_pricempire` | 34 | 34 |
| `no_drift` | 28 | 28 |
| `no_comparable_data` | 10 | 8 |
| `pattern_skip` | 10 | 5 |
| `drift_alert` | 2 | 2 |

**Phase B — "fresh" (cycle ending :46)** — sampled at 2026-05-17 20:46:32 UTC:

| verdict | n | distinct items |
|---|---|---|
| `no_drift` | 62 | 35 |
| `no_comparable_data` | 10 | 8 |
| `pattern_skip` | 10 | 5 |
| `drift_alert` | 2 | 2 |

In Phase A, the entire skinport pair (34 items) reports `stale_pricempire`; in Phase B those same 34 items report `no_drift`. The dmarket pair, `pattern_skip`, `no_comparable_data`, and `drift_alert` counts are stable across phases — the alternation is exclusive to the skinport pair's freshness gate.

Latest-cycle split by curated source (Phase A, the more interesting one because it makes the asymmetry visible):

| curated | verdict | n |
|---|---|---|
| `dmarket` | `no_drift` | 28 |
| `dmarket` | `no_comparable_data` | 7 |
| `dmarket` | `pattern_skip` | 5 |
| `dmarket` | `drift_alert` | 2 |
| `skinport` | `stale_pricempire` | 34 |
| `skinport` | `pattern_skip` | 5 |
| `skinport` | `no_comparable_data` | 3 |

Pair totals: `dmarket` = 42, `skinport` = 42. ✓

In Phase A the skinport pair has **zero `no_drift` and zero `drift_alert`** — every fresh-data skinport-pair item shows `stale_pricempire`. In Phase B those same items become `no_drift`. The skinport pair is structurally under-covered for drift detection (§3.a and §3.1 quantify the consequence).

### §2.b Supporting: 21h 30min aggregate

Over all 44 cycles:

| verdict | n | % |
|---|---|---|
| `no_drift` | 2008 | 54.3% |
| `stale_pricempire` | 782 | 21.2% |
| `pattern_skip` | 440 | 11.9% |
| `no_comparable_data` | 440 | 11.9% |
| `drift_alert` | 26 | 0.7% |

The 21.2% `stale_pricempire` is the time-average of the bimodal pattern: ~22 Phase-A cycles × 34 stale_pricempire rows + ~22 Phase-B cycles × 0 stale_pricempire rows ≈ 748 / 3696 = 20.2% (rough check; actual 21.2% reflects a few extra Phase-A-leaning cycles early on). The aggregate is not "noisy convergence to a mean" — it's the average of two stable phases the cycle alternates between.

`stale_both` and `stale_curated` are absent from the data — zero rows for both across all 44 cycles. The curated side has never been stale beyond the 30-min threshold during this window.

**The 0.7% `drift_alert` headline is misleading.** §3.1 breaks it down: 25 of the 26 alert rows are 2 stuck dmarket-pair divergences; the skinport pair surfaced exactly one alert in the entire window, despite Phase B making the skinport pair drift-checkable in ~half of all cycles. The effective alert rate on dmarket is ~5% of items; on skinport it's ~0% empirically (but the §3.a coverage gap means we likely missed events).

---

## §3. Cycle-by-cycle evolution — the structural bimodal pattern locked in from cycle 1

Per-cycle verdict timeline (every cycle, abbreviated):

```
cycle           no_drift  drift_alert  pattern_skip  stale_pricempire  no_comp
2026-05-16 23:16    30          0           10             34             10  ← Phase A
2026-05-16 23:46    64          0           10              0             10  ← Phase B
2026-05-17 00:16    30          0           10             34             10
2026-05-17 00:46    64          0           10              0             10
...
2026-05-17 13:46    29          1           10             34             10
2026-05-17 14:16    63          1           10              0             10
2026-05-17 14:46    29          1           10             34             10
...
2026-05-17 16:16    28          2           10             34             10
2026-05-17 16:46    62          2           10              0             10
...
2026-05-17 20:16    28          2           10             34             10
2026-05-17 20:46    62          2           10              0             10
```

**The structural bimodal distribution locked in from cycle 1.** Cycle 1 is Phase A (high-stale, 34/30/10/10/0); cycle 2 is Phase B (low-stale, 0/64/10/10/0); every subsequent cycle alternates. There was no monotonic warmup-to-steady transition — the detector landed directly on its operating shape. The 21h aggregate that averages 21.2% `stale_pricempire` is the time-average of two stable phases, NOT convergence to a unimodal steady state. Readers reasoning about detector behavior cycle-by-cycle should not assume a flat distribution; the verdict shape any given consumer sees depends on which phase they sampled.

The two drift-alert step-ups (cycle 27 → 1 alert; cycle 35 → 2 alerts) reflect new persistent divergences arriving; the alerts persist once they arrive (see §3.1).

Three structural observations:

**`pattern_skip` is rock-stable at 10 from cycle 1.** Five `phase_based` items (3 Dopplers + Karambit Marble Fade FN + Karambit Tiger Tooth FN, per `data/pattern_sensitivity.yaml`) × 2 pairs = 10 `pattern_skip` rows per cycle. The classifier wired correctly on the first cycle and has never deviated.

**`no_comparable_data` is rock-stable at 10 from cycle 1.** Sample (latest Phase-A cycle, 8 distinct items, 10 rows because Souvenir AWP Dragon Lore FN and BS each contribute both pairs):

| item | pair | curated_price | pricempire_price |
|---|---|---|---|
| `AK-47 \| Hydroponic (FN)` | dmarket | NULL | $5,299.00 |
| `AK-47 \| Wild Lotus (FN)` | skinport | NULL | $16,146.59 |
| `AWP \| Desert Hydra (FN)` | dmarket | NULL | $2,799.00 |
| `Desert Eagle \| Blaze (FN)` | dmarket | NULL | $743.21 |
| `MP7 \| Bloodsport (FT)` | dmarket | NULL | $32.72 |
| `SSG 08 \| Death Strike (FN)` | dmarket | NULL | $625.24 |
| `Souvenir AWP \| Dragon Lore (BS)` | dmarket | NULL | NULL |
| `Souvenir AWP \| Dragon Lore (BS)` | skinport | NULL | NULL |
| `Souvenir AWP \| Dragon Lore (FN)` | dmarket | NULL | NULL |
| `Souvenir AWP \| Dragon Lore (FN)` | skinport | NULL | NULL |

8 rows are "curated side missing, Pricempire has a number" (mostly DMarket items that still fall through `iterate-objects[]` — see §5); 2 rows on `Souvenir AWP Dragon Lore BS/FN` have both sides missing. The Souvenir-Lore-FN entry didn't exist in the watchlist before Step 7.1 (a new Tier 3 add); the BS variant was in the original watchlist. Both genuinely lack listings on direct collectors AND in Pricempire's response.

**`drift_alert` grew slowly: 0 → 1 → 2 over the window.** First `drift_alert` fired at 02:16 UTC (cycle 7, ★ Sport Gloves Hedge Maze FT, skinport pair, +36% — single cycle only); then 13:46 (cycle 28, M4A4 Buzz Kill FT crossed the 10% threshold on dmarket pair, has fired every cycle since); then 16:16 (cycle 35, Sport Gloves Pandora's Box FT joined on dmarket pair, also persistent). §3.1 characterizes all 26 alert rows in detail.

### §3.a The `stale_pricempire` oscillation — empirical, structural, but not fully characterized

**Per-item age delta at latest cycle (Phase B, 20:46:32 UTC).** For each of the 40 deep items that have both pairs scored (the 2 missing items are `Souvenir AWP | Dragon Lore (BS)` and `(FN)` — both `no_comparable_data` on both pairs, so `pricempire_age_min` is NULL for both pairs):

| metric | dmarket_age (min) | skinport_age (min) | skinport − dmarket (min) |
|---|---|---|---|
| p10 | 2.4 | 10.9 | 8.5 |
| p50 (median) | 2.4 | 10.9 | 8.5 |
| p90 | 2.4 | 10.9 | 8.5 |
| min | 2.4 | 10.9 | 8.5 |
| max | 2.4 | 10.9 | 8.5 |
| stddev | 0.00 | 0.00 | 0.00 |

The per-item variance is **zero**. All 40 items share the same `pricempire_dmarket` age (2.4 min) and the same `pricempire_skinport` age (10.9 min). This means each Pricempire collector cycle writes `pricempire_observation_log` rows for a given sub-provider as a synchronized batch — all items in the cycle's response get the same wire-supplied `last_checked_at` within stddev=0 resolution.

The 8.5-min delta in Phase B is much smaller than what Phase A saw. Sample meta_info from a stale_pricempire row in the most recent Phase-A cycle (20:16:32):

```
curated:                skinport, 17.2 min old (fresh)
pricempire_skinport:    54.7 min old (stale by the 30-min gate)
pricempire_dmarket (same item, same cycle): 2.4 min old (fresh)
curated_last_polled:    2026-05-17 19:59:22
pricempire_last_polled: 2026-05-17 19:21:52
```

So at Phase A, the skinport pair's effective age was 54.7 min; at Phase B (30 min later), it was 10.9 min. The age dropped because a fresh Pricempire skinport batch landed between the two cycles. The historical skinport ages (19:21:52 → 20:35:38) imply Pricempire's upstream skinport refresh cadence in this window is somewhere in the **~30-75 min range**, not the 15-min cadence the dmarket pair shows.

**What's pinned down with high confidence:**
- Per-cycle, per-sub-provider age variance across items is essentially zero (stddev 0.00 at p10/p50/p90 — all 40 items move together).
- The skinport pair lags the dmarket pair by **anywhere from ~8 min to ~55 min within a single drift cycle**, depending on which phase the cycle sampled.
- The alternation between Phase A and Phase B is structural and reproduced every cycle pair (44/44 cycles fit the pattern).
- `collectors/pricempire.py:481-486` writes `pricempire_observation_log.last_observed_at = wire_row["last_checked_at"]` (Pricempire's upstream timestamp, not our local poll time), which is the architecturally correct choice (ADR 023's separation-of-clocks design) and is the mechanism producing the visible asymmetry.

**What is NOT pinned down (warrants more data before threshold revision):**
- The precise Pricempire-skinport upstream refresh cadence. n=2 historical data points (19:21:52 → 20:35:38 = 73 min apart) suggest a cadence in the 30-75 min range, but a longer window is needed to characterize the distribution (is it stable? Bursty? Time-of-day dependent?).
- Whether the dmarket-pair ~2.4 min age is itself representative or a snapshot artifact. A longer window would show whether dmarket's cadence is uniformly tight or also has occasional gaps.
- The other four sub-providers (`buff163`, `buff163_buy`, `csmoney`, `swap_gg`) are not in the meaningful-pair set, but a parallel cadence characterization would inform whether to ever expand the pair set later.

**Implications for ADR 022 — calibrate cautiously:**
- Document the per-sub-provider Pricempire upstream cadence as a load-bearing operational fact backed by zero-variance per-cycle batch evidence.
- The 30-min `STALE_PRICEMPIRE_MINUTES` constant is mis-calibrated for the skinport pair as currently observed; raising it would reduce false-positive `stale_pricempire`. But §3.1 shows the cost of getting this wrong: the skinport pair surfaced only 1 drift_alert in 21h, vs the dmarket pair's 25. Effective skinport-side detection coverage in steady state is at most ~50% of cycles (the Phase-B half).
- Recommend a follow-up investigation cycle (e.g., 7-day window) to characterize the upstream cadence distribution before changing the threshold, rather than jumping to "raise to 75 min" off this single window's evidence. ADR 022 should document the calibration question as an open follow-up, not close it.

### §3.1 Characterization of all `drift_alert` rows in the window

26 alert rows across the 21h 30min window, breaking down as:

| item | pair | n cycles | drift % | classification | curated price | pricempire price | first alert | last alert |
|---|---|---|---|---|---|---|---|---|
| `M4A4 \| Buzz Kill (FT)` | dmarket | 15 | +10.46% (stuck) | pattern_agnostic | $275.00 | $248.97 | 2026-05-17 13:46 | 2026-05-17 20:46 |
| `★ Sport Gloves \| Pandora's Box (FT)` | dmarket | 10 | +13.16% (stuck) | pattern_agnostic | $4,299.98 | $3,800.00 | 2026-05-17 16:16 | 2026-05-17 20:46 |
| `★ Sport Gloves \| Hedge Maze (FT)` | skinport | 1 | +36.08% (one-shot) | pattern_agnostic | $5,963.27 | $4,382.07 | 2026-05-17 02:16 | 2026-05-17 02:16 |

Three items, two sub-providers, very heavy clustering on the dmarket pair.

**The two persistent dmarket alerts have *literally identical* drift values across every alert cycle** — Buzz Kill at 0.1046 for all 15 cycles, Pandora's Box at 0.1316 for all 10 cycles. Drift mathematically can't be that stable unless both the curated price and the Pricempire price are unchanged for the duration. Two non-mutually-exclusive explanations:
1. Both sides are genuinely stable. The DMarket listing's `.price.USD` field hasn't moved, and Pricempire's dmarket-source view also hasn't moved. Real flat markets do this; high-priced items with thin liquidity especially.
2. Both sides are showing dedup-induced freshness illusion: `prices.timestamp` hasn't advanced because nothing changed, but `observation_log.last_observed_at` is fresh (pre-dedup). The drift detector reads the latest `prices.price` value, which is whatever it was when the price last actually moved. This is the architecturally correct behavior (ADR 017), but it means a "stuck" drift alert can reflect either real persistent divergence or a frozen feed.

Both items in question are in the "premium, thin-liquidity" bucket (Sport Gloves Pandora's Box at $4.3k, Buzz Kill at $275 with low volume) where genuine days-long flat periods are plausible. The drift detector's job is to surface the divergence; distinguishing "real flat" from "frozen feed" would require cross-referencing the count/volume fields or a separate freshness gate that includes the underlying source's volume signal. Out of scope for v1; worth noting for ADR 022.

**The one skinport alert** (Hedge Maze, +36% drift, single cycle at 02:16:32) is a one-shot detection. It fired in cycle 7 (the second `:16` cycle, Phase A) and did not repeat. Possible explanations: (a) Pricempire's skinport price subsequently corrected so the gap dropped below 10%; (b) the Hedge Maze item moved into `stale_pricempire` from cycle 8 onward and we lost coverage; (c) the curated skinport price moved into line. We can't distinguish from the drift_verdict data alone — the rows for Hedge Maze after cycle 7 would have to be inspected individually. (Not done here; flagging for §7 follow-up.)

**Strategic implications for ADR 022:**
- The 0.7% raw `drift_alert` rate breaks down as ~5% of dmarket-pair items (2 of 42) with persistent divergences + ~0% of skinport-pair items (1 of 42 one-shot in the entire window). The skinport-pair rate is suppressed by the §3.a coverage gap.
- 25 of 26 alerts (96%) are dmarket-pair. The detector is empirically producing useful divergence signal on the dmarket pair and almost no signal on the skinport pair. Threshold recalibration (§3.a) and the "stuck vs real" distinction above both directly affect the alert-rate story.
- The two persistent items (Buzz Kill, Pandora's Box) are plausible divergences and worth a human spot-check against the live markets — neither is a known canary failure mode (no DMarket title mismatch involvement, no phase-bearing pattern, classification correctly `pattern_agnostic`). They are the kind of finding the detector exists to surface.

---

## §4. Wear-disambiguation behavior (Step 7.1 swaps)

The two Step 7.1 swaps:
- `USP-S | Neo-Noir (Field-Tested)` moved from orphan-equivalent (not previously watchlisted) to deep; `USP-S | Neo-Noir (Factory New)` moved from deep to orphan (originally watchlisted, now dropped).
- `AWP | Dragon Lore (Factory New)` moved into deep; `AWP | Dragon Lore (Field-Tested)` moved to orphan.

Drift verdicts written per wear in the 21h 30min window:

| market_hash_name | expected tier | drift_verdict rows | latest verdict |
|---|---|---|---|
| `AWP \| Dragon Lore (Factory New)` | deep | 88 | (in latest cycle) |
| `AWP \| Dragon Lore (Field-Tested)` | orphan | **0 (canary)** | — |
| `USP-S \| Neo-Noir (Factory New)` | orphan | **0 (canary)** | — |
| `USP-S \| Neo-Noir (Field-Tested)` | deep | 88 | (in latest cycle) |

44 cycles × 2 pairs = 88 expected for deep ✓.

**Regression canary:** the orphan rows must be **exactly zero** in any future validation cycle. A non-zero count would indicate the drift detector's deep-set load (`compute_and_store` reading YAML's `tier: deep` filter — `analytics/drift.py:398-404`) has regressed, or that the watchlist YAML schema changed without the detector adapting. Non-zero on these specific four items is the fast-failing signal for either bug class because they are the Step 7.1 wear swaps and exist precisely to exercise both sides of the tier boundary.

Curated `observation_log` freshness for the same four items:

| item | source | last_polled_at | age |
|---|---|---|---|
| AWP DL FN (deep) | dmarket | 2026-05-17 20:30:04 | 5.6 min |
| AWP DL FN (deep) | skinport | 2026-05-17 20:29:22 | 6.3 min |
| AWP DL FT (orphan) | dmarket | 2026-05-16 22:41:35 | 21h 54m (pre-deploy, frozen) |
| AWP DL FT (orphan) | skinport | 2026-05-16 22:52:59 | 21h 42m (pre-deploy, frozen) |
| USP-S NN FN (orphan) | dmarket | 2026-05-16 22:44:29 | 21h 51m (pre-deploy, frozen) |
| USP-S NN FN (orphan) | skinport | 2026-05-16 22:52:59 | 21h 42m (pre-deploy, frozen) |
| USP-S NN FT (deep) | dmarket | 2026-05-17 20:31:58 | 3.7 min |
| USP-S NN FT (deep) | skinport | 2026-05-17 20:29:22 | 6.3 min |
| USP-S NN FT (deep) | steam_market | 2026-05-17 17:36:03 | 3h (intermittent) |

Both deep wears are being polled fresh by both DMarket and Skinport. Both orphan wears stopped being polled at deploy time (their last-polled timestamps are all from ~30 min before the deploy, consistent with their final pre-deploy collector cycle). The `_load_watchlist` tier filter from Step 7.1.5 is end-to-end working.

The USP-S Neo-Noir FT row's stale Steam observation (3h old) is unrelated — Steam's tier filter is deep-only, but Steam's rate-limit pauses can leave individual items un-refreshed for hours within a single cycle window. Not a Phase 2b finding.

---

## §4.5 Tier-inertness sanity check

The deep set is the 42 `tier: deep` entries in `data/watchlist.yaml`; orphans are the 28 items in `items` table whose `market_hash_name` is not in the deep set. (Tier lives in YAML, not the items table, per ADR 024.)

| | count |
|---|---|
| items table | 70 |
| deep set (YAML) | 42 |
| orphans | 28 |

### Collector side — clean (regression canary: 0)

| source | orphan obs_log rows | advanced in 21h 30min window |
|---|---|---|
| `dmarket` | 28 | **0 (canary)** |
| `skinport` | 28 | **0 (canary)** |
| `steam_market` | (none in obs_log) | **0 (canary)** |

Zero advancements. `_load_watchlist`'s tier filter (`collectors/scheduler.py`, Step 7.1.5) is honestly inert for orphans across all three curated collectors. Zero new `prices` rows for orphans in the window across any source.

**Regression canary:** the "advanced in window" column must be **exactly zero** in any future validation cycle. Non-zero would indicate `_load_watchlist` regressed (e.g., reverted to the pre-Step-7.1.5 `SELECT FROM items` query, or the YAML tier filter broke). The Pricempire collector intentionally still reads orphans (its `_load_item_index` reads `items` directly, preserving the "orphan data stays warm" invariant per ADR 024) and is therefore excluded from this canary — it's expected to keep advancing `pricempire_observation_log` for orphans.

### Drift detector side — clean (regression canary: 0)

Zero `drift_verdict` rows for orphans in the window (verified for all 28 orphans). The drift detector reads its deep-set from YAML, not from the items table (`analytics/drift.py:398-404`), so orphans are correctly skipped.

**Regression canary:** zero orphan `drift_verdict` rows must hold in any future validation cycle. Non-zero would indicate the detector's deep-set load broke or that `tier:` field semantics changed in the YAML. Two of the four §4 wear-swap items are orphans precisely to make this canary fail loudly if anyone changes the loader without thinking through tier filtering.

### Other analytics jobs — **not tier-aware**

Orphan items continue to receive new rows from other analytics insight types in the window:

| insight_type | orphan rows written in 21h window |
|---|---|
| `cross_source_spread` | 143 |
| `cross_source_view` | 160 |
| `item_unavailability_streak` | **6,328** |
| `moving_avg_7d` | 1,288 |
| `moving_avg_30d` | 1,288 |

`cross_source_spread`, `cross_source_view`, `moving_avg_*`: these compute over historical observations, and since orphans have pre-Step-7.1 history in `prices`, the jobs continue to produce rows. Whether this is desired (historical analytics-on-demand) or a bug (insights for items the user can't see in their watchlist anymore) is a design question for ADR 024.

`item_unavailability_streak` is the more pointed case. The streak increments every cycle for every (orphan × source) pair where the source hasn't produced an observation recently. Orphans will never produce another observation, so the streak counters grow unbounded forever. 6,328 rows in 21h is ~28 orphans × ~22 cycles × ~10 source-flavors of streak rows; the rate is steady-state, not warmup. At 21h/day this is ~7,200 rows/day, ~2.6M rows/year just for orphan unavailability streaks.

This is a Phase 2b tier-awareness gap that didn't get scoped into the Step 7.1.5 filter (which scoped only to collector-side polling). ADR 024 should call out tier-awareness as a cross-cutting concern that should propagate beyond the collector. A TODO.md entry is warranted to track per-job tier-filter scoping.

---

## §5. DMarket `iterate-objects[]` empirical (ADR 012 §7 follow-up)

All 8 items from ADR 012 §7's table are currently `tier: deep` (none are orphan). DMarket polling resumed at deploy time (Step 6 was rolled into the foundation commit 11aaf25, which was the first to ship the `iterate-objects[]` change). Status in the 21h window:

| item | tier | DMarket obs_log last polled | new DMarket prices rows |
|---|---|---|---|
| `Desert Eagle \| Blaze (FN)` | deep | (none) | 0 |
| `M4A1-S \| Cyrex (FT)` | deep | 2026-05-17 20:30:31 (fresh) | 44 |
| `MP9 \| Hot Rod (FN)` | deep | 2026-05-17 20:31:21 (fresh) | 26 |
| `SSG 08 \| Death Strike (FN)` | deep | (none) | 0 |
| `Souvenir AWP \| Dragon Lore (BS)` | deep | (none) | 0 |
| `★ Butterfly Knife \| Fade (FN)` | deep | 2026-05-17 20:32:09 (fresh) | 23 |
| `★ Huntsman Knife \| Fade (FN)` | deep | 2026-05-17 20:32:19 (fresh) | 27 |
| `★ Karambit \| Fade (FN)` | deep | 2026-05-17 20:32:30 (fresh) | 14 |

**Exact match against ADR 012 §7's prediction.** 5 items that ADR 012 §7 predicted would start producing prices (M4A1-S Cyrex, MP9 Hot Rod, Butterfly Fade, Huntsman Fade, Karambit Fade) are now producing prices, with fresh observation_log timestamps. 3 items that ADR 012 §7 predicted would remain unavailable (Desert Eagle Blaze, SSG Death Strike, Souvenir AWP Dragon Lore BS) have no observation_log row and no prices in the window — exactly as documented in the ADR's empirical table.

The per-item `new DMarket prices rows` count varies (14-44) — these are post-dedup writes, so the variance reflects how often the item's `(price, volume)` actually moved within the window, not a collector health issue.

Currently-passing deep items unaffected: spot-checked AWP Dragon Lore FN (deep, newly added) — fresh DMarket polling, fresh prices rows. The iterate-objects[] change did not regress items that were already working.

---

## §6. Pricempire metadata + observations coverage for the 22 newly-added items

The 22 items added in Step 7.1 (11 rifles, 6 snipers, 2 pistols, 3 SMGs). Coverage check is twofold: do they have rows in `pricempire_item_metadata` (per-item, slow-changing — rank, liquidity, marketcap, trade volumes), and do they have rows in `pricempire_observations` (per-item × per-sub-provider — price + count)?

**Headline:** all 22 are covered on both. Specifically:

- **22 of 22** have at least one `pricempire_item_metadata` row.
- **22 of 22** have at least one `pricempire_observations` row.

These are different claims. The first means Pricempire surfaced the item at all in its catalog. The second means at least one sub-provider returned a non-null price.

Per-item drill:

| item | metadata rows | sub-providers with obs (max 6) | total obs rows |
|---|---|---|---|
| `Souvenir AWP \| Dragon Lore (FN)` | 1 | 2 | 4 |
| `MP9 \| Starlight Protector (FT)` | 1 | 6 | 87 |
| `AWP \| Dragon Lore (FN)` | 11 | 5 | 38 |
| `AK-47 \| Hydroponic (FN)` | 12 | 6 | 19 |
| `M4A1-S \| Blue Phosphor (FN)` | 12 | 6 | 67 |
| `AWP \| Gungnir (FN)` | 13 | 5 | 22 |
| `M4A4 \| Buzz Kill (FT)` | 13 | 6 | 56 |
| `AWP \| Desert Hydra (FN)` | 14 | 5 | 59 |
| `AWP \| Fade (FN)` | 15 | 6 | 63 |
| `M4A1-S \| Mecha Industries (FT)` | 15 | 6 | 56 |
| `MP7 \| Bloodsport (FT)` | 15 | 6 | 64 |
| `SSG 08 \| Dragonfire (FT)` | 15 | 6 | 63 |
| `USP-S \| Orion (FN)` | 15 | 6 | 60 |
| `AK-47 \| Wild Lotus (FN)` | 16 | 5 | 20 |
| `M4A4 \| Poseidon (FN)` | 16 | 5 | 44 |
| `AK-47 \| Gold Arabesque (FN)` | 16 | 6 | 53 |
| `M4A1-S \| Hot Rod (FN)` | 16 | 6 | 54 |
| `M4A4 \| Neo-Noir (FT)` | 17 | 6 | 91 |
| `AK-47 \| Neon Revolution (FT)` | 18 | 6 | 69 |
| `P90 \| Asiimov (FT)` | 18 | 6 | 58 |
| `USP-S \| Neo-Noir (FT)` | 18 | 6 | 55 |
| `AK-47 \| Bloodsport (MW)` | 20 | 6 | 98 |

17 of 22 have all 6 sub-providers represented; 5 of 22 cover only 5 of 6 (typical thin-coverage where one sub-provider's listings come back null).

Two outliers:

- **`Souvenir AWP | Dragon Lore (FN)`** — 1 metadata row, 2 sub-providers, 4 observation rows. This is a brand-new Tier 3 add and the *highest-end* Souvenir DLore in the catalog. Consistent with Phase 2a validation §2's "extremely low-liquidity item" characterization for the BS variant; the FN version is even thinner.
- **`MP9 | Starlight Protector (FT)`** — only 1 metadata row but 87 observations across all 6 sub-providers. The metadata dedup tuple (rank, liquidity, marketcap, count, trade volumes) is extremely stable on this item — quantized to the same 11-field tuple every cycle, so the gate suppresses all but the first write per ADR 020.

The metadata row counts (1-20 over 21 hours of 15-min Pricempire cycles ≈ 84 max possible writes) are consistent with Pricempire's per-cycle metadata churn — most items see slow rank/liquidity drift that triggers occasional writes, with the rate depending on how active the item is in the broader market.

---

## §7. Coexistence-rule status — and a latent key-name mismatch

`cross_source_divergence` fired in the window:

| metric | value |
|---|---|
| divergence rows in 21h window | 87 |
| distinct items with at least one divergence | 20 |
| first divergence | 2026-05-17 06:16:32 UTC |
| latest | 2026-05-17 20:16:32 UTC |

Spot-check: a sample `cross_source_divergence` row's full `meta_info`:

```json
{
    "n_samples": 23,
    "source_a_id": "9",
    "source_b_id": "10",
    "threshold_z": 2,
    "baseline_mean": 0.0907,
    "baseline_stddev": 0.0122,
    "observed_spread": 0.0539
}
```

**The meta_info carries `source_a_id` and `source_b_id`, but NOT `source_a_name` or `source_b_name`.** This matters because the bot's Step 9 coexistence filter (`bot/tools.py:417-419`) reads:

```python
sa = meta.get("source_a_name", "")
sb = meta.get("source_b_name", "")
return sa.startswith("pricempire_") or sb.startswith("pricempire_")
```

The filter keys off field names that aren't populated on `cross_source_divergence` rows. `sa` and `sb` always default to `""`; `"".startswith("pricempire_")` is always False; the filter never fires.

**Today this is harmless.** `cross_source_spread` is curated-only by construction (`analytics/drift.py:52-60` documents this; verified by query — `cross_source_spread` rows in the window pair only `steam_market`, `skinport`, `dmarket`). `cross_source_divergence` is computed from `cross_source_spread`, so it inherits curated-only by construction. There is no Pricempire-involving divergence row today.

**The latent risk:** the defense-in-depth filter doesn't actually defend. If a future change adds Pricempire pairs to `cross_source_spread`, Pricempire-involving divergences will leak into the bot's `anomaly_flag` rendering instead of being suppressed in favor of `drift_summary`. The Step 9 commit message explicitly framed the filter as "defense-in-depth against future schema changes that could let Pricempire spreads leak into the legacy anomaly_flag" — but that defense relies on key names that don't exist.

Three remediation paths (deferred to TODO.md):
1. Extend `analytics/anomaly_detection.py:182-189` to write `source_a_name` / `source_b_name` alongside the IDs.
2. Rewrite the bot filter to look up source names from `source_a_id` / `source_b_id` via the API (or via a cached source-id-to-name map).
3. Drop the filter and rely on the "curated-only by construction" guarantee, with a regression test that pins the invariant.

The legitimate `cross_source_divergence` rows (87 in the window, 20 items) are the curated cross-source spread anomalies the analytics layer was designed to surface (e.g., a real-money Skinport-vs-DMarket spread that diverged from its rolling baseline by ≥ 2σ). These flow through the bot's `anomaly_flag` rendering as intended — none have been incorrectly suppressed.

---

## §8. Findings and ADR hand-off

**For ADR 022 (drift detector):**
- The detector is operating as designed on the dmarket pair: rock-stable cadence (§1), real `drift_alert` surfacing on plausible items (§3.1's Buzz Kill + Pandora's Box), `pattern_skip` correctly skipping all 5 phase_based items every cycle (§3). No quantitative pre-deploy prediction exists; ADR 022's "empirical findings" section is characterization, not validation against a forecast.
- The bimodal Phase-A / Phase-B oscillation (§2.a, §3) is structural, not transient. ADR 022 should document the distribution as bimodal-per-cycle rather than describing a single steady-state distribution. Per-cycle variance across items is zero (§3.a percentile table); the structure is the upstream Pricempire cadence asymmetry, not detector noise.
- `STALE_PRICEMPIRE_MINUTES = 30` is empirically mis-calibrated for the skinport pair — §3.a's per-cycle delta evidence (8.5 min within Phase B, 54.7 min within Phase A) shows skinport rows are routinely older than 30 min through no fault of our pipeline. ADR 022 should document this as a known calibration gap with the recalibration deferred pending a longer-window characterization. Jumping straight to "raise to 75 min" risks misreading the single window's worth of evidence; recommend a 7-day follow-up window before changing the constant. §3.1 shows the cost of the current setting: skinport-pair alert rate empirically ~0% vs dmarket-pair ~5%.
- The "stuck divergence" pattern from §3.1 (identical drift values across 15 / 10 cycles for Buzz Kill / Pandora's Box) is a known consequence of dedup-on-write combined with `prices.timestamp` only advancing when the value changes. ADR 022 should document that a "drift_alert that persists across cycles with the same drift value" is the expected shape for a real flat-market divergence; distinguishing it from a frozen feed is out of scope for the detector itself.

**For ADR 024 (two-tier architecture):**
- Collector tier-inertness on orphans is confirmed end-to-end (§4.5). Cite §4 (wear-swap behavior) and §5 (DMarket items all deep) as proof the YAML-driven tier filter is the canonical authority.
- **Open scope question:** the tier filter applies only at the collector and at the drift detector, not at the other analytics jobs. §4.5 quantifies the gap (6,328 unavailability_streak rows / 21h for orphans, plus moving_avg / cross_source rows). ADR 024 should either claim tier-awareness as a cross-cutting concern with a phase plan, or document this as out-of-scope with rationale.

**For TODO.md (will be added in 10c):**
- Pricempire-refresh asymmetry triage (per-sub-provider cadence characterization, threshold revision).
- Analytics-side tier filter scoping (especially `item_unavailability_streak` unbounded-growth on orphans).
- Coexistence filter key-name mismatch (three remediation paths in §7).

**For the existing TODO.md entries:**
- The sources-table cadence drift item (Phase 2b Step 2 filing) remains current and unresolved; this validation window confirms `dmarket` and `skinport` are both at 30/5 (the server-default backfill) rather than the migration 0003 intended 15/0 and 15/3. Doesn't affect §1's drift cadence (which is its own scheduler).
