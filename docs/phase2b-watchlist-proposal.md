# Phase 2b watchlist proposal — DRAFT, awaits human review

**Status:** Proposal only. **Phase 2a does NOT re-seed the watchlist.** This document is the deliverable; the actual re-seed happens in Phase 2b during the drift-detection work.

The current 48-item watchlist was curated ad-hoc for v1 (`data/watchlist.yaml`). With Pricempire ingest live (Phase 2a) and drift detection landing in Phase 2b, the watchlist's composition becomes load-bearing — drift detection works only against items where we have good direct-poll coverage AND good Pricempire coverage. This proposal lays out a tier-based composition (Tier 1, 2, 3, 5 — the original Tier 4 was deferred; see "Selection criteria" below) that you can accept, edit, or reject before re-seeding.

## Data sources

- `data/watchlist.yaml` — the current 48-item v1 watchlist.
- `docs/pre-phase2-pricempire-samples/metas-sample*.json` — Pricempire `/v4/paid/items/metas` snapshot from Phase 0 diagnostic. 91,294 items with `rank`, `liquidity`, `marketcap`, `steam_last_*` trade volumes.
- `pricempire_observations` table — live ingest of the current watchlist (6 providers per item, ~17 min of data at the time of writing). Used to verify per-provider priced-coverage for each candidate.
- `docs/phase2a-ingest-validation.md §3` — flagged Doppler-pattern items (Karambit / Flip Knife / M9 Bayonet Doppler FN) as drift-detection problems. Excluded from this proposal.

## Selection criteria

- **Tier 1** — top items by Pricempire `rank`, excluding Doppler patterns. High popularity AND high liquidity. These anchor drift detection against the deepest cross-provider data.
- **Tier 2** — mid-tier items in the $50-$500 price range with `liquidity ≥ 50`. Moderate trade volume keeps drift signals non-zero but ratios stay sensible.
- **Tier 3** — illiquid premium items (Souvenir Dragon Lores, rare gloves, premium knives). Exercises the long-tail drift path; price drift here is real news.
- **Tier 5** — known-problematic items the existing watchlist already carries (the 8 DMarket title-mismatch casualties). Keep as drift canaries — if Pricempire's DMarket data appears here, it proves the title-mismatch is a *direct-collector* problem, not an *upstream-availability* problem.

**Steam-only canaries deferred.** Stickers, music kits, and patches were considered as a Tier 4 but deferred — Pricempire doesn't price them meaningfully, and the Steam collector's behavior on non-skin item types hasn't been characterized. A small future phase can verify Steam collector coverage on these item types and add them then if useful.

Target total: **41 items** — within the 35-50 band the brief asked for.

## Tier 1 — high-volume liquid (15 items)

| market_hash_name | Pricempire rank | liquidity | Pricempire priced coverage |
|---|---|---|---|
| M4A4 \| Buzz Kill (Field-Tested) | 1 | 91 | 6/6 |
| M4A1-S \| Hot Rod (Factory New) | 2 | 50 | 6/6 |
| SSG 08 \| Dragonfire (Field-Tested) | 6 | 90 | 6/6 |
| ★ Butterfly Knife \| Fade (Factory New) | 5 | 59 | 6/6 |
| ★ Sport Gloves \| Hedge Maze (Field-Tested) | 8 | 52 | 6/6 |
| AK-47 \| Hydroponic (Factory New) | 9 | 52 | 6/6 |
| ★ Sport Gloves \| Pandora's Box (Field-Tested) | 10 | 56 | 6/6 |
| AWP \| Dragon Lore (Factory New) | 11 | 82 | 5/6 (no swapgg) |
| M4A1-S \| Blue Phosphor (Factory New) | 12 | 59 | 6/6 |
| Desert Eagle \| Blaze (Factory New) | 13 | 56 | 6/6 |
| M4A1-S \| Printstream (Field-Tested) | 14 | 72 | 6/6 |
| AWP \| Gungnir (Factory New) | 15 | 81 | 5/6 (no swapgg) |
| P90 \| Asiimov (Field-Tested) | 16 | 93 | 6/6 |
| AK-47 \| Redline (Field-Tested) | 19 | 100 | 6/6 |
| AK-47 \| Neon Revolution (Field-Tested) | 20 | 93 | 6/6 |

Note: I excluded ranks 3, 4, 7 (M4A4 Buzz Kill MW, SSG 08 Dragonfire MW/FN) because the same skin appears at rank 1 and 6 in another wear — keeping one wear per skin maximizes coverage breadth without over-indexing on a single design.

## Tier 2 — mid-tier (12 items)

Selection: rank 50-300, liquidity > 50, $50-$500 price range, ≥5 priced providers. Selected to spread across weapon families (rifle, sniper, pistol, knife, glove).

| market_hash_name | rank | liquidity |
|---|---|---|
| AK-47 \| Bloodsport (Minimal Wear) | 52 | 69 |
| M4A4 \| Poseidon (Factory New) | 53 | 51 |
| USP-S \| Orion (Factory New) | 55 | 63 |
| AWP \| Fade (Factory New) | 56 | 54 |
| AK-47 \| Vulcan (Factory New) | 57 | 59 |
| M4A4 \| Neo-Noir (Field-Tested) | 58 | 100 |
| AK-47 \| Gold Arabesque (Factory New) | 60 | 75 |
| AWP \| Desert Hydra (Factory New) | 62 | 81 |
| M4A1-S \| Mecha Industries (Field-Tested) | 69 | 93 |
| MP7 \| Bloodsport (Field-Tested) | 72 | 95 |
| MP9 \| Starlight Protector (Field-Tested) | 76 | 100 |
| USP-S \| Neo-Noir (Field-Tested) | 78 | 89 |

## Tier 3 — illiquid premium (6 items)

These items have low Pricempire `liquidity` scores or are explicitly low-listing-count. Drift here is real news.

| market_hash_name | rank | liquidity | Pattern-sensitivity risk | Notes |
|---|---|---|---|---|
| Souvenir AWP \| Dragon Lore (Factory New) | 178 | 0 | — | Liquidity 0 means almost no live listings — pure canary. |
| ★ Karambit \| Marble Fade (Factory New) | 89 | 62 | Doppler-like (phase variants) | Knife premium. Already in current watchlist. |
| ★ Karambit \| Tiger Tooth (Factory New) | 118 | 65 | Doppler-like (phase variants) | Knife premium. |
| AK-47 \| Wild Lotus (Factory New) | 30 | 75 | — | Premium rifle. |
| M4A4 \| Howl (Factory New) | 35 | 79 | — | Contraband collectible. Already in current watchlist. |
| ★ Sport Gloves \| Vice (Field-Tested) | 114 | 76 | Pattern-seed (rare seeds skew price) | Premium gloves. |

The "Pattern-sensitivity risk" column flags items where the underlying upstream taxonomy mismatch documented in `docs/phase2a-ingest-validation.md §3` is likely to recur. These items are deliberately included as candidates for the pattern-sensitivity classifier that Phase 2b will introduce — drift detection on them will need different thresholds (or to be skipped entirely) until the classifier is in place. They are not bad picks; they are test cases for the new classifier work.

## Tier 5 — known-problematic canaries (8 items)

Keep the existing 8 DMarket title-mismatch casualties. They're already in the current watchlist. Crucially, Pricempire's all-6-providers probe shows ALL 8 have priced coverage — which means **the title-mismatch is purely a problem with our direct DMarket collector's title-matcher, not an upstream-data-availability problem**. The fix scope is settled in "Phase 2b directions" below: repair the direct collector's title-matcher (alias map), do NOT swap the direct DMarket source for Pricempire's DMarket view.

| market_hash_name | Pricempire all-providers priced coverage |
|---|---|
| Desert Eagle \| Blaze (Factory New) | 6/6 |
| M4A1-S \| Cyrex (Field-Tested) | 6/6 |
| MP9 \| Hot Rod (Factory New) | 6/6 |
| SSG 08 \| Death Strike (Factory New) | 6/6 |
| Souvenir AWP \| Dragon Lore (Battle-Scarred) | 6/6 (2/6 in live ingest after 13.5h — extremely low-liquidity item, expected per validation §2) |
| ★ Butterfly Knife \| Fade (Factory New) | 6/6 |
| ★ Huntsman Knife \| Fade (Factory New) | 6/6 |
| ★ Karambit \| Fade (Factory New) | 6/6 |

Per `docs/phase2a-ingest-validation.md §6`, `swap_gg`'s apparent sparse ingest (45 rows over 13.5 h) was diagnosed as the dedup gate working correctly on a low-liquidity sub-marketplace, not an ingest bug. All 44 swap_gg-covered watchlist items show DB values matching Pricempire's current values exactly. No action needed at the watchlist composition level.

Note: Souvenir AWP Dragon Lore BS shows up as having only 2 providers in `pricempire_observations` after 2 cycles (per Step 4 validation §2). The all-6 probe ran ~30 min earlier and showed 6/6. Possible explanations: (a) some providers dropped the listing between probes, (b) Pricempire returns the row but some providers have null prices intermittently. Phase 2b's first cycle of drift logic should clarify which.

## Cross-tier diffs against the current 48-item watchlist

**Keeping from current watchlist** (15 of 48 items survive into the proposal):

- AK-47 \| Redline (Field-Tested), AK-47 \| Hydroponic (FN)
- AWP \| Dragon Lore (FN)
- Desert Eagle \| Blaze (FN)
- M4A1-S \| Hot Rod (FN), M4A1-S \| Printstream (FT)
- M4A4 \| Howl (FN)
- ★ Butterfly Knife \| Fade (FN), ★ Huntsman Knife \| Fade (FN), ★ Karambit \| Fade (FN), ★ Karambit \| Marble Fade (FN)
- SSG 08 \| Dragonfire (FT), SSG 08 \| Death Strike (FN)
- MP9 \| Hot Rod (FN)
- M4A1-S \| Cyrex (FT)

**Dropping from current watchlist** (33 items):

The dropped items are mostly the v1 ad-hoc picks that don't appear in Pricempire's top-300 rank. They're not bad items per se — they just don't anchor drift detection well. The most notable drops:

- All Doppler items (3) — Karambit Doppler FN, Flip Knife Doppler FN, M9 Bayonet Doppler FN. Reason: Doppler-phase aggregation produces 60-74% drift between direct Skinport and Pricempire's Skinport (Step 4 validation §3). They'd dominate Phase 2b's drift signal with noise.
- Karambit Doppler MW, M9 Bayonet Crimson Web FT, Karambit Crimson Web FN, Bayonet Marble Fade FN, Karambit Doppler FN dups (different waveforms) — premium knife variants that overlap functionally with Karambit Tiger Tooth FN / Marble Fade FN in the proposal.
- AK-47 \| Asiimov (BS), AK-47 \| Slate (FT), AK-47 \| Fire Serpent (FT), AK-47 \| Redline (MW) — covered by AK-47 Redline FT, Hydroponic FN, Bloodsport MW, Vulcan FN in the proposal.
- AWP \| Hyper Beast (FT), AWP \| Lightning Strike (FN), AWP \| Containment Breach (FT), AWP \| Redline (FT), AWP \| Neo-Noir (FT), AWP \| Asiimov (FT) — covered by AWP Dragon Lore FN, Gungnir FN, Fade FN, Desert Hydra FN in the proposal.
- M4A4 \| Asiimov (FT) — covered by M4A4 Buzz Kill FT, Neo-Noir FT, Poseidon FN, Howl FN in the proposal.
- USP-S \| Kill Confirmed (FT), USP-S \| Neo-Noir (FN) — Neo-Noir FT and Orion FN cover this slot.
- Glock-18 \| Fade (FN) — pistol coverage replaced by Desert Eagle Blaze FN, USP-S Orion FN, USP-S Neo-Noir FT.
- The various Karambit/Bayonet/M9 Bayonet patterns that aren't Doppler — knife coverage shifts to Karambit Marble Fade FN, Tiger Tooth FN, Butterfly Knife Fade FN, Huntsman Knife Fade FN.
- The various Sport Gloves wears — covered by Hedge Maze FT, Pandora's Box FT, Vice FT in the proposal.

**Adding to current watchlist** (20 new items):

All Tier 1 except the 4 already in the current watchlist, plus all 12 Tier 2 items, plus 4 new Tier 3 items.

## Phase 2b directions (settled, not open)

**DMarket title-mismatch fix scope.** Tier 5 demonstrates that Pricempire's DMarket data is available for all 8 problematic items. The fix in Phase 2b is to repair the direct collector's title-matcher (most cleanly: a per-item alias map in `data/watchlist.yaml`), NOT to swap the direct DMarket source for Pricempire's DMarket view. The direct DMarket collector is the independent verification leg against Pricempire's DMarket data; losing it would collapse two independent observations into one. Pricempire serving DMarket data for these items proves the upstream is available — the problem lives purely in our title-matcher.

## Summary

| Tier | Count | Theme |
|---|---|---|
| Tier 1 | 15 | High-volume liquid, top-20 rank |
| Tier 2 | 12 | Mid-tier rifles / snipers / pistols, rank 50-300 |
| Tier 3 | 6 | Illiquid premium: Souvenir Lore, rare gloves, premium knives |
| Tier 5 | 8 | Known-problematic canaries: the DMarket title-mismatch set |
| **Total** | **41** | |

## Open questions for human review

1. **Doppler items entirely excluded vs. include with a flag.** I excluded all Doppler-pattern items because the upstream taxonomy mismatch produces big drift signals that Phase 2b would need to learn to ignore. The alternative is to keep one Doppler (e.g. Karambit Doppler FN) as a known-bad anchor for the drift logic to learn against. Tradeoff is "cleaner data" vs "phases-bearing canary."
2. **Souvenir AWP Dragon Lore Battle-Scarred coverage discrepancy.** Pricempire's all-6-probe says 6/6 priced; live ingest after 2 cycles says only 2 providers wrote rows. Worth one more probe before deciding whether to keep this Tier-5 canary or replace it. Probably keep — it's only 1/8 of Tier 5.
3. **Total size 41 is comfortably mid-band of the 35-50 target.** If you want to trim to ~38, dropping 2 Tier-2 and 1 Tier-3 items reads cleanest. If you want to grow to 50, the next-most-defensible additions are ranks 21-30 (already known to be 6/6 covered, just not Doppler-clean).
4. **The current watchlist has explicit YAML comments and section markers.** The re-seed will need to preserve those via `ruamel.yaml` per the existing `scripts/watchlist_edit.py` pattern. The proposal here is shape-only; the actual YAML refactor is a Phase 2b implementation detail.

---

**Phase 2a's job ends here.** No re-seed has been performed. The current `data/watchlist.yaml` is unchanged. Phase 2b's first task should be: review this proposal, accept/edit/reject each tier, then execute the re-seed.
