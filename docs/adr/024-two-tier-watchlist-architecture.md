# ADR 024 — Three-tier watchlist architecture

**Status:** Accepted (Phase 2c update: tier vocabulary renamed
deep/broad/orphan → curated/featured/substrate, schema_version 2→3.
The §3 Addendum recording the Path B v1 implementation is
pre-Phase-2c-Commit-2 state and will be rewritten in the next commit
to record Path A selection — leave the prior text in place as the
audit trail of the path-selection deferral.)
**Date:** 2026-05-17 (original); 2026-05-18 (Phase 2c rename pass)
**Related:** ADR 005 (slug algorithm, v1 collision class), ADR 009
(scheduler design, dedup-on-write), ADR 014 (read API design), ADR
016 (Discord bot runtime), ADR 018 (Pricempire as breadth-coverage
source), ADR 020 (Pricempire item-metadata hypertable), ADR 021
(pattern-sensitivity classifier — planned, not yet written), ADR
022 (pattern-aware drift detector), ADR 023 (pricempire_observation_log
— planned, not yet written).

**Retrospective note (2026-05-18, Phase 2c rename).** The original
draft used the vocabulary "deep / broad / orphan." Phase 2c renamed
these to "curated / featured / substrate" to reflect post-Path-A
semantics where the items table is bulk-populated and the YAML's
tracked-list is the editorial overlay rather than the catalog floor.
This ADR has been edited in place to use the new vocabulary
throughout; historical narrative references (e.g., references to
the original proposal's "Tier 1 / 2 / 3 / 5" or to Step 7.1's
"orphan items") keep the period-appropriate vocabulary with
parenthetical mapping where needed. The schema_version bump 2→3
is the on-disk marker of the rename. Pre-Phase-2c readers should
understand: deep = curated, broad = featured, orphan = substrate.

## Context

The v1 watchlist was a single hand-curated 48-item list in
`data/watchlist.yaml` polled end-to-end by all collectors. Phase 2a
landed Pricempire as a breadth-coverage source (ADR 018) — one HTTP
call covers all ~39,400 CS2 items, with six sub-providers per item.
That breadth lives on a separate hypertable
(`pricempire_observations`) and is not co-mingled with curated
prices. The curated path (Steam / Skinport / DMarket per-item polls)
is structurally bounded by rate limits; Pricempire is structurally
bounded by an HTTP-call-per-cycle budget and arbitrary catalog
coverage.

Phase 2b's first artifact was `docs/phase2b-watchlist-proposal.md` —
a tier-1/2/3/5 re-seed plan, 41 items total, scoped to direct-poll
fidelity. The proposal explicitly excluded all Doppler-pattern items
(60-74% Skinport-vs-Pricempire-Skinport drift, would dominate the
drift signal). It also explicitly carried no notion of a broad tier:
the words "Tier 4" were considered (Steam-only canaries: stickers,
patches, music kits) and deferred.

Two things changed between proposal and implementation:

1. **The classifier was added.** ADR 021's pattern-sensitivity
   classifier landed alongside the drift detector (ADR 022). With
   `phase_based` items emitting `pattern_skip` regardless of drift
   ratio, the noise concern that justified excluding Dopplers from
   the proposal was structurally addressed. Three Dopplers
   (Karambit / M9 Bayonet / Flip Knife Doppler FN) were
   re-introduced as curated-tier items with `phase_based` classification.
   They produce a stable 10 `pattern_skip` rows/cycle and zero noise
   in the drift signal.
2. **The "broad tier" notion crystallized.** Pricempire's bulk-call
   pattern made it cheap to track several hundred more items at zero
   incremental HTTP cost. A broad tier was added as a first-class
   concept in the YAML schema (bump to `schema_version: 2`, per-item
   `tier:` field, `_VALID_TIERS = {"deep", "broad"}`). But the
   bootstrap path — "pick top-N by Pricempire rank from a catalog
   we haven't yet ingested into the items table" — had a
   chicken-and-egg gap (§3 below). Step 7.1 shipped with the
   infrastructure live and the featured-tier population empty.

What's on disk after Phase 2c bootstrap (2026-05-18):

```
data/watchlist.yaml:
    schema_version: 2
    broad_tier_exclusions: 2 entries     # slug-v1 collision pair
                                         # (Sunset Storm 壱/弐 FN);
                                         # see §3.1
    items:
      - {…, tier: curated}   × 42  entries
      - {…, tier: featured}  × 500 entries  # Phase 2c, Path B v1

DB items table:
    545 rows = 42 deep + 500 broad + 3 orphans
    (orphan count dropped 28 → 3 at Phase 2c — 25 previously-deep
    items re_added to broad per §4.D5; 3 ranked outside top-500
    and remain orphan.)

analytics/pattern_classifier.py:
    build_classifier(...) raises ValueError on any classifier
    entry whose market_hash_name has tier: featured.
```

The original (Step 7.1, 2026-05-17) state was:
```
broad_tier_exclusions: []
items: deep×42, broad×0
items table: 70 rows = 42 deep + 28 orphans
```
The §3 resolution preserves the original deferral framing for
audit; §3 Addendum (Phase 2c) records the close-out and the path
selection.

This ADR records the shape, reconciles proposal-vs-implementation,
and resolves five outstanding policy decisions. The bootstrap gap
is **not** resolved here — it is consciously deferred, and §3
documents that deferral so a future phase has the framing in hand.

## Proposal-vs-implementation reconciliation

> **Vocabulary note.** This table documents what was decided/shipped
> at Phase 2b Step 7.1 (May 17, 2026) using the original tier
> vocabulary `deep / broad / orphan`. Phase 2c renamed these
> `curated / featured / substrate` (schema_version 2→3). Per the
> retrospective note at the top of this ADR, historical narrative
> stays in period-appropriate vocabulary; the mapping is global:
> deep → curated, broad → featured, orphan → substrate.

| Aspect | Proposal | Shipped | Delta |
|---|---|---|---|
| Total deep items | 41 stated; 39 unique (Desert Eagle Blaze FN and ★ Butterfly Knife Fade FN each appeared in both Tier 1 and Tier 5) | 42 | +3 Dopplers re-introduced as `phase_based` |
| Doppler items | Excluded (taxonomy noise) | Included (3, all `phase_based`) | ADR 021's classifier removed the original reason for exclusion |
| Tier vocabulary | Tier 1 / 2 / 3 / 5 | `deep` + `broad` (slot, currently empty); `orphan` computed at read time | "Tier" semantics collapsed to the operationally meaningful distinction: deep = polled by all four collectors and drift-evaluated; broad = polled only by bulk sources; orphan = was deep, no longer in YAML |
| Broad tier (~500 items) | Not in proposal | Slot exists; zero items today | Added as a Phase-2b decision; bootstrap deferred (§3) |
| Tier 4 (Steam-only canaries) | Deferred | Deferred | Unchanged |
| Source filtering by tier | Not specified | Implemented in `collectors/scheduler.py:_load_watchlist` and `_CURATED_ONLY_SOURCES` | Steam + DMarket gated to deep tier by rate-limit math (5s/item × 500 items > 60-min Steam cycle; 3s/item × 500 items > 15-min DMarket cycle); Skinport + Pricempire bulk-fetch deep + broad |
| Orphan handling | Implicit ("the dropped items") | Explicit `Tier = Literal["deep", "broad", "orphan"]` | 28 rows in `items` table from the prior 48-item watchlist with `prices` / `pricempire_observations` / `observation_log` / `insights` history preserved |
| Migration shape | Not specified | 0009 is a no-op stub; tier lives in YAML, not the items table | YAML-as-source-of-truth was preferred over a denormalized column (§4 D1) |
| DMarket title-mismatch fix | Per-item alias map in YAML (proposal §"Phase 2b directions") | Implemented as `dmarket_alias:` optional list per deep item; warns on featured-tier use as dead config | Matches proposal scope |

Net deep composition: 41 proposal items → 39 unique after deduplicating
the two T1∩T5 overlaps → 39 + 3 Dopplers re-introduced = 42 shipped.
Orphans = items in DB but not in the new YAML = 28. Broad = 0.

## §3. The featured-tier bootstrap chicken-and-egg

`scripts/seed_broad_tier.py` reads `pricempire_item_metadata` to pick
the top-N items by Pricempire rank, joining `items` to translate
`item_id` → `market_hash_name`:

```sql
SELECT DISTINCT ON (m.item_id)
    m.item_id, i.market_hash_name, m.rank, m.liquidity
FROM pricempire_item_metadata m
JOIN items i ON i.id = m.item_id
WHERE m.rank IS NOT NULL
ORDER BY m.item_id, m.timestamp DESC
```

The metadata table is populated by the Pricempire collector as a
side effect of each price-ingest cycle (ADR 020). But the price-
ingest cycle only persists rows for items already present in
`items` (ADR 018 §6 — "Phase 2a only ingests Pricempire data for
curated-watchlist items"). And `items` is populated from the YAML.

**The cycle:**
- Picking the top-N featured-tier items by rank requires `pricempire_item_metadata` to have rows for the candidate items.
- Having rows for candidate items requires those items to be in `items`.
- Having an item in `items` requires it to be in the YAML.
- Putting it in the YAML requires having picked it.

Three escape paths exist, each with non-trivial side effects:

| Path | What it changes | Cost |
|---|---|---|
| **A. Bulk-seed `items`** from a Pricempire `/v4/paid/items/metas` snapshot — write all ~39,400 catalog rows into the items table once, then let the seeder pick top-N from a full population. | Items table grows from 70 to ~40,000 rows; every existence query against the items table (bot, API, analytics) sweeps a much larger table; orphan/broad/deep semantics for the bulk-seeded items become ambiguous (none would be in the YAML; all would be "orphan" by current rules). | High blast radius — touches the bot's "I don't track that item" message, the API's 404 handling, and the orphan envelope copy. |
| **B. Change `seed_broad_tier` to read raw Pricempire snapshots** (e.g. a fresh HTTP call to `/v4/paid/items/metas`, or a snapshot table backed by a special bootstrap collector run that bypasses the items-table filter). | seeder becomes network-dependent or schema-dependent on a bootstrap table that doesn't exist; the rank source diverges from the production `pricempire_item_metadata` table. | Medium — adds a third moving part to a script the operator is supposed to run quarterly. |
| **C. Defer broad tier until a follow-up phase explicitly designs the bootstrap path.** | Zero new code, zero new data, zero new tables. The featured-tier slot stays empty in production; all surrounding infrastructure (loaders, API responses, bot tier_note copy, exclusion list, scheduler filtering) exercises only by tests with synthetic broad items. | Low — the cost is "we ship two-tier without using both tiers today." |

**Resolution: path C, with the gap documented here.** The bootstrap
is a one-shot operator workflow that doesn't need to be on the
critical path for Phase 2b. The two-tier architecture is correct
in shape regardless of whether the broad tier has 0 or 500 items
today; the schema, the loaders, the API, the bot, and the scheduler
all behave correctly with broad = ∅. **Phase 2c is scoped to select
and implement one of paths A or B**, at which point this ADR's
deferral closes; until that work lands, the C-temporary stance
holds.

What this ADR resolves about the deferral:

- **The deferral is a known-finite gap, not a TODO.** When the
  bootstrap phase lands, it will pick one of paths A or B; this ADR
  is not pre-committing.
- **Test coverage for featured-tier code paths is the cost of the
  deferral.** Production has zero broad items, so the YAML loader,
  the scheduler filter, the API tier branching, and the bot
  envelope copy can only be exercised by tests injecting synthetic
  broad items. Tests must inject; production smoke does not cover.
- **`broad_tier_exclusions:` ships as `[]`.** The exclusion list is
  hand-maintained and never written by the seeder; an empty list is
  the correct shape for an empty broad tier, and the seeder's
  "added when first run" behavior won't accidentally repopulate
  exclusions.
- **`data/watchlist.yaml` is the single tier-membership source.**
  When the bootstrap lands and featured-tier items appear in the YAML,
  no schema migration is required — the loader already understands
  `tier: featured`, and the YAML format already has the slot.

The gap is shaped so a future phase pays the cost once when it has
a use case in hand, not now when the use case is hypothetical.

### §3 Addendum (Phase 2c, 2026-05-18) — deferral closed; Path A selected; commit 1 landed rename, commit 2 lands the bulk-seed

Phase 2c selected **Path A**: bulk-seed the items table with the
top ~5,000 Pricempire catalog rows (by rank) so the items table
becomes the catalog substrate that the YAML's tracked-list overlays.
Rationale is forward-looking:

- **Storage substrate for the post-v1 on-demand-fetch + auto-
  promotion feature.** Phase 3+ scope (its own ADR will land then)
  includes a Discord-bot path where users ask about an untracked
  item, the bot fetches it on the spot, and a repeat-popularity
  signal can auto-promote it to featured tier. Path A's bulk-pre-
  seed means existence queries against `items` return a row for
  any catalog item, which makes that future flow a fetch-and-update
  rather than an existence-create-fetch three-step. The on-demand
  feature itself is NOT in scope for Phase 2c; only the storage
  decision is.
- **Substrate semantics become semantically honest.** Pre-Phase-2c
  the items table held only the curated 42 + 28 orphans (a remnant
  set, not a catalog floor). Path A makes "substrate" the natural
  default state — a Pricempire-only catalog row with no curated
  history. The earlier "orphan" framing (a remnant of editorial
  drops) collapses into one of two substrate subtypes; the bot
  envelope copy is generic across both.

Path B variants are rejected in favor of Path A:

- **Path B variant 1 (fresh HTTP per seeder run; the working prior
  approach).** Lowest blast radius (one new function, no
  items-table bulk-grow), but doesn't pre-stage the substrate for
  the on-demand-fetch flow. Yesterday's session (2026-05-17 →
  2026-05-18) implemented this path before the advisor coordination
  surfaced the forward-looking Path A rationale. The Path B v1
  implementation was rolled back in this commit; the 500-item YAML
  population it produced is preserved (the YAML is unchanged in
  composition).
- **Path B variant 2 (bootstrap-snapshot table).** Adds a third
  moving part to a script the operator runs quarterly. The snapshot
  cache pays no operational dividend at this cadence; not
  pre-committed.

Phase 2c implementation lands across two commits:

- **Commit 1 (this commit).** Tier vocabulary renamed
  `deep / broad / orphan` → `curated / featured / substrate`
  (schema_version 2→3); `item_unavailability_streak` analytics
  removed entirely (TODO.md "item_unavailability_streak removal");
  retrospective addenda on validation + proposal docs; doc
  sanitize audit; `notes/` gitignored; session-handoff prompts
  drafted at `notes/slug-v2-session-prompt.md` and
  `notes/path-a-bulk-seed-session-prompt.md`. Items table is
  unchanged at 545 rows (42 curated + 500 featured + 3 substrate)
  from yesterday's Path B v1 bootstrap output; canaries from
  `docs/phase2b-validation.md §4 / §4.5` continue to hold
  post-rename.
- **Commit 2 (pending).** Path A bulk-seed via new
  `scripts/seed_catalog.py`: single transaction, fail-fast in
  dry-run on slug collisions, top-5000-by-rank ingestion. Items
  table grows 545 → ~5,000. Canary re-verification + this §3
  Addendum gets a final Path-A-implementation update. Full scope
  + pause-points at `notes/path-a-bulk-seed-session-prompt.md`.

The 500-item featured tier in `data/watchlist.yaml` survives both
commits unchanged — it's the rank-driven popularity layer over
whichever items table state exists. The "via Path B v1" framing in
its bootstrap is replaced by "via Path A's pre-seeded catalog
floor + seed_featured_tier.py's DB-source path" once commit 2
lands.

Items table state across this transition:

| When | Curated | Featured | Substrate | Total |
|---|---|---|---|---|
| Pre-Phase-2b (May 16) | 48 | 0 | 0 | 48 |
| Post-Step-7.1 (May 17) | 42 | 0 | 28 | 70 |
| Post-Path-B-v1 bootstrap (May 18 ~00:40) | 42 | 500 | 3 | 545 |
| Post-commit-1 (this commit; rename only) | 42 | 500 | 3 | 545 |
| Post-commit-2 (Path A bulk-seed) | 42 | 500 | ~4,458 | ~5,000 |

Numbers in the substrate column are the inverse of the YAML's
tracked-list against the items table; the row count grows with
each bulk-seed, but the editorial decisions (curated, featured)
stay editorial.

### §3.1 Slug-v1 collision surfaced at bootstrap

ADR 005 §"Consequences" anticipated this exactly:

> Unique-constraint collisions are theoretically possible if two
> distinct market_hash_names produce the same slug. At v1
> watchlist size we have zero collisions; the constraint will
> surface any future collision loudly as an insert error.

The bootstrap surfaced one collision pair:
- `Desert Eagle | Sunset Storm 壱 (Factory New)` (rank 379, "Sunset Storm I")
- `Desert Eagle | Sunset Storm 弐 (Factory New)` (rank 405, "Sunset Storm II")

The slug-v1 algorithm strips non-ASCII characters at step 5
(`_NON_SLUG_CHAR.sub("", s)`), so 壱 and 弐 both vanish, leaving
both names with the identical slug `desert-eagle-sunset-storm-factory-new`.
The items table's `slug` UNIQUE constraint correctly rejected the
second insert with `psycopg.errors.UniqueViolation`. The failed
INSERT happened in `scripts/seed_watchlist.py`'s single
transaction, so the partial-write rolled back cleanly — items
table state was unchanged after the failed run.

**Resolution (interim):** Both colliding items added to
`broad_tier_exclusions:` in `data/watchlist.yaml` with a comment
pointing at slug v2. The seeder filled the freed slots from rank
537-538 (AUG Syd Mead FT, Desert Eagle Ocean Drive MW). 500
featured-tier items total, no collisions, items table populated
cleanly.

**Resolution (strategic):** ADR 005 v2 — disambiguation rule for
non-ASCII characters in slugs. Open follow-up. The exclusion-list
entries are the natural exit hatch: when slug v2 lands, the
exclusions get removed, the seeder picks both Sunset Storm wears
back up at the next run, the items table accepts both inserts.

Only one collision pair surfaced in the 500-item broad set;
checked exhaustively via Python against the post-bootstrap YAML.
No other Japanese-character or non-Latin-script items appeared in
the top-500-by-rank pool. The collision surface scales with
featured-tier size; at ~500 items it's manageable as an exclusion-list
operator workflow, but slug v2 closes the door.

## §4. Five decisions

ARCHITECTURE.md rule: each decision below is preceded by a one-sentence
statement of what it is supposed to accomplish, then resolved.

### §4.D1 YAML-vs-DB-vs-hybrid for the featured-tier list

**What this decision is supposed to accomplish:** establish a single
source of truth for tier membership so that the loader, the
scheduler, the analytics pipelines, the API, and the bot all read
the same view of "which items are deep, broad, or orphan."

**Choice: YAML-only.** Tier membership lives in
`data/watchlist.yaml`'s per-item `tier:` field. The `items` table
does NOT have a tier column; migration 0009 is a no-op stub
documenting that the change is YAML-side only.

**Rejected alternatives:**

- **Tier column on the `items` table.** Would split the source of
  truth: edits to the YAML would not take effect until
  `seed_watchlist.py` ran, and edits to the DB column would not
  back-propagate to the YAML. The current YAML-loader-reads-on-
  startup pattern (`api/watchlist_tiers.py`,
  `analytics/drift.py`, `collectors/scheduler.py:_load_watchlist`,
  `analytics/pattern_classifier.py`) keeps the YAML authoritative.
- **Hybrid: tier in YAML, denormalized to items.tier on seed.**
  Rejected: denormalization without a write-back path invites
  drift; the YAML's `tier:` is small and cheap to re-read at
  collector / API / analytics startup. A future "reduce startup
  cost" optimization can move this without a schema break, but the
  benefit-to-risk ratio today says no.
- **Separate `broad_tier` table.** Splits broad and deep across two
  storage layers, makes promotion/demotion (§4.D5) require two
  writes, and forces every tier-aware query to UNION.

**How this is enforced:**

- `scripts/seed_watchlist.py:_SUPPORTED_SCHEMA_VERSION = 3`; the
  loader rejects schema_version mismatch.
- `scripts/seed_watchlist.py:_VALID_TIERS = frozenset({"curated", "featured"})`
  is the closed set; any other tier value fails fast at load.
- `data/watchlist.yaml`'s `featured_tier_exclusions:` is also YAML-only;
  the seeder reads but never writes it (operator-maintained veto
  list).
- An operator YAML edit needs a service restart to take effect
  (matches the alias-map reload discipline of ADR 012 §7 and the
  classifier reload discipline of ADR 022 §2.6).

### §4.D2 Classification policy for featured-tier items

**What this decision is supposed to accomplish:** define whether
pattern-sensitivity classification (ADR 021) applies to featured-tier
items, so that a future operator adding broad items knows what
classifications mean across tiers.

**Choice: classifier fail-fasts on featured-tier entries.** The
pattern-sensitivity classifier is meaningful only for items eligible
for drift detection; drift detection is deep-only (§4.D3); therefore
a classifier entry on a featured-tier item is dead config and the
loader rejects it at startup.

**Implementation:**
`analytics/pattern_classifier.py:build_classifier` (§"Layer 2") raises
`ValueError` with "tier: featured in data/watchlist.yaml — drift
detection is deep-only" when a classifier entry's
market_hash_name appears in `items_set` but not in `curated_set`. This
is one of three named fail-fast modes (the other two being UNKNOWN
ITEM and MISSING TIER FIELD).

**Why fail-fast rather than warn-and-ignore:**
A classifier entry on a featured-tier item is operationally meaningless
— the drift detector will never see the item — but it would be
silently meaningless. The operator who added the entry probably
intended for it to do *something*. A startup ValueError surfaces the
intent mismatch immediately; a per-cycle warn would either spam logs
or get suppressed and miss.

**Rejected alternative: classifier entries silently ignored for
broad.** Would create a class of dead config that looks alive in
the YAML, with no feedback loop telling the operator their
intentions weren't honored.

**Note: `pattern_agnostic` is the implicit default for items not in
`pattern_sensitivity.yaml`.** A featured-tier item with no classifier
entry is `pattern_agnostic` by default (multiplier 1.0). This is
harmless because drift detection never runs against broad items
regardless of classification. The fail-fast applies only to *explicit*
classifier entries on featured-tier items.

### §4.D3 Per-insight tier-awareness policy

**What this decision is supposed to accomplish:** define for each
existing analytics insight type whether it computes against deep
only, broad-inclusive, or tier-agnostic — so consumers (bot, API)
know which insight kinds exist for which tier classes.

**Choice: per-insight rule, applied per tier.** The original
single-column "tier scope" framing collapsed two distinct
behaviors — *can the analytics job operate on data that exists at
all* (the broad question) vs. *does the analytics job stop
operating on an item once it's dropped from the YAML* (the orphan
question). They have different answers per insight type because
broad has no curated history (computation impossible) while orphan
typically has curated history from its pre-drop curated-tier days
(computation possible, and currently *happening* per
`docs/phase2b-validation.md §4.5`).

Three columns, grounded in §4.5's measured 21h-window orphan
volumes:

| Insight type | Deep | Broad | Orphan | §4.5 orphan rows (21h) |
|---|---|---|---|---|
| `drift_verdict` | ✓ | ✗ (no curated side) | ✗ filtered by `curated_set` YAML read in `analytics/drift.py:398-414` | 0 (canary, §4.b) |
| `cross_source_spread` | ✓ | ✗ (curated × curated, both deep-only sources) | **Writes orphan rows** — computes over historical `prices` rows; pre-Step-7.1 deep history is still present. Filter decision deferred (§4.D3-TODO). | **143** |
| `cross_source_view` | ✓ | Partial (one-row-per-source over whatever sources priced the item) | **Writes orphan rows** — same historical-compute reason as `cross_source_spread` | **160** |
| `cross_source_divergence` | ✓ | ✗ (built from `cross_source_spread`, inherits curated-only) | **Inherits cross_source_spread's orphan behavior** — derived from spread rows, so will write divergence rows for orphans that produced spreads in the window. Not separately tabulated in §4.5; same filter decision as spread. | (derived) |
| `moving_avg_7d` / `moving_avg_30d` | ✓ | Tier-agnostic when broad has Pricempire price rows | **Writes orphan rows** — pure historical compute over `prices`. Bounded by window size (no unbounded-growth risk; rolls forward, doesn't accumulate). | **1,288** each |
| `item_unavailability_streak` | ✓ | Behavior on broad untested (no broad items in production) | **Writes orphan rows AND grows unbounded** — increments every cycle for every (orphan × source) pair where the source hasn't produced an observation recently. Orphans never produce another observation, so the streak counters grow forever. Per-job tier filter needed (§4.D3-TODO). | **6,328** (~2.6M/year projected) |
| `volume_anomaly` | ✓ | Tier-agnostic when broad has price rows | Same historical-compute story; not separately tabulated in §4.5 | (not measured) |
| `news_correlated_move` (future) | ✓ | ✗ (requires curated multi-source view) | TBD when implemented | n/a |
| Narrative job | Tier-agnostic input, deep-focused output | Reads all `insights` but the daily paragraph naturally surfaces deep because that's where divergence/drift signals live | Reads orphan insight rows transparently; rendering inherits whatever upstream jobs produced | n/a |

**§4.D3-TODO — analytics-side orphan filter scope.** Two of the
five orphan-writing insight types are bounded (the moving-average
pair rolls forward over a fixed window; the cross-source views
compute over the same historical window the spreads use). One is
*unbounded*: `item_unavailability_streak` accumulates one row per
(orphan × source) per cycle forever. Per §4.5's measurement: 6,328
rows in 21h ≈ 28 orphans × ~22 cycles × ~10 source-flavors,
steady-state, not warmup. Projected at ~2.6M rows/year just for
orphan unavailability streaks today, and that ratio scales linearly
with orphan count — see §4.D5 and Consequences for the
broad-population interaction.

A per-job tier filter pass is the right shape of fix (each job
reads the YAML deep-set at cycle start and skips orphans). The
work is scoped out of this ADR — three of the five jobs need only
historical-compute filter decisions (cheap), one needs a
streak-reset semantic (does dropping an item to orphan reset the
streak to zero, freeze it at its last value, or stop incrementing?
each has different downstream consequences). A follow-up TODO
entry tracks the scoping.

**Implementation today:**
- `analytics/drift.py:compute_and_store` filters items to
  `curated_set = {item["market_hash_name"] for item in watchlist["items"] if item.get("tier") == "curated"}`
  at cycle start. Zero `drift_verdict` rows for non-deep items by
  construction. The validation doc's regression canary
  (`docs/phase2b-validation.md §4.b / §4.5`) checks this invariant
  continues to hold across cycles.
- `analytics/anomaly_detection.py` (cross_source_spread /
  divergence) iterates source pairs and filters by source-side
  only — orphans with pre-drop curated history continue producing
  rows. Empirically 143 spread rows / 21h for orphans per §4.5;
  the structural broad-exclusion holds, the orphan-exclusion does
  not.
- `analytics/cross_source.py` (cross_source_view) — same shape;
  160 orphan rows / 21h per §4.5.
- `analytics/moving_averages.py` — source-agnostic and
  tier-agnostic by design; 1,288 + 1,288 = 2,576 orphan rows / 21h
  per §4.5. Window-bounded so doesn't accumulate.
- `analytics/unavailability_streak.py` — no tier filter; orphan
  rows grow unbounded. **This is the load-bearing gap.**

**Rejected alternative: gate every insight type on `tier == "curated"`
unconditionally.** Would suppress legitimate moving-average and
volume-anomaly signals on broad items — both *can* be computed
meaningfully on Pricempire-only data. The per-job decision matters
because broad and orphan have structurally different data
availability.

### §4.D4 API and bot surface for tier

**What this decision is supposed to accomplish:** ensure the read
API and the bot surface tier as a first-class field so consumers
can distinguish data-quality differences (deep has multi-source
curated data + drift; broad has Pricempire-only; orphan has only
history) without parsing display names or guessing from absence of
fields.

**Choice:**

- **`Tier = Literal["curated", "featured", "substrate"]`** is part
  of `api/schemas.py` and is returned on every item-facing response
  (`Item`, `ItemDetail`, `PriceResponse`, `HistoryResponse`,
  `DriftResponse`, `DealResponse`).
- **Tier resolution lives in `api/watchlist_tiers.py:get_tier()`.**
  Returns `"curated"` or `"featured"` for in-YAML items,
  `"substrate"` for items the caller has already verified exist
  in the `items` table but are not in the YAML. Caches the YAML
  on first call; reload on `api` service restart.
- **404 vs substrate split happens at the route layer.** Each
  route queries `items` for existence (the 404 path); on hit,
  calls `get_tier()` to decide tier branching. The 422 path is
  rejected for non-curated tier on `/drift` (the caller asked a
  sensible question about a known item; empty answer is
  structural — same precedent as `/deals/evaluate` returning 200
  with `verdict="no_comparable_data"`).
- **Bot envelope copy is centralized.** `bot/tools.py:_attach_tier_envelope`
  injects `tier_note` (and `active_wear_hint` for substrate items
  with a sibling curated-tier wear) into every tool result whose
  API response carries `tier != "curated"`. Two pre-composed
  strings: `_TIER_NOTE_FEATURED` and
  `_tier_note_substrate(active_wear)`. The LLM renders verbatim —
  defensive against the open-source model inventing the wrong
  framing (ADR 016 rationale).

**Why three values and not two:**
"Orphan" is observationally distinct from both deep and broad. A
deep item has a curated cross-source view today. A broad item has
Pricempire-only view today. An orphan item has *no current data*
but may have historical prices, drift verdicts, and observation
logs accumulated before it was dropped from the YAML. Collapsing
orphan into broad would tell the bot "we track this with less
detail" when the truth is "we no longer track this at all."

**Why bot copy is pre-composed instead of LLM-generated:**
ADR 016's defensive-handling rationale: the abliterated Qwen3
model invents reasonable-sounding but wrong context if left to
generate tier explanations from scratch. The cost of two fixed
strings is small; the cost of "we don't have current prices but
the model said we do" is high.

### §4.D5 Promotion / demotion semantics

**What this decision is supposed to accomplish:** define the
operator workflow for moving items between tiers and the data-
lifecycle semantics so the operator knows what survives and what
disappears across the moves.

**Choice:** three movement rules, applied independently.

**Deep ↔ broad (operator-edited).**
- Deep tier is editorial. `scripts/watchlist_edit.py` or hand edit
  of the `items:` block in YAML, then `git commit`. The seeder
  (`seed_broad_tier.py`) NEVER touches deep tier — its allow-list
  is broad only.
- A deep item demoted to broad just changes its `tier:` field. The
  next scheduler restart stops Steam + DMarket polls for that item;
  Skinport + Pricempire continue. Existing prices / drift rows /
  observation_log rows in the DB are preserved.
- A broad item promoted to deep gains Steam + DMarket polls on the
  next scheduler restart; the items table already has the row
  (featured-tier seed wrote it via `seed_watchlist.py`'s
  `ON CONFLICT DO NOTHING`).
- **Operator checklist on broad → deep promotion** (not enforced by
  code today; a future `scripts/watchlist_edit.py` enhancement
  could automate the first item):
  - Add `dmarket_alias:` entries if the canonical Steam name
    differs from DMarket's title for that item (see ADR 012 §7).
    Missing aliases produce zero DMarket rows silently for that
    item until the alias lands.
  - Verify the item has Pricempire `pricempire_skinport` and
    `pricempire_dmarket` coverage if drift detection is wanted;
    items with sparse Pricempire data will surface
    `no_comparable_data` verdicts (acceptable, but worth knowing).
  - Consider whether the item belongs in
    `data/pattern_sensitivity.yaml` — phase-bearing items
    (Dopplers, Marble Fade, etc.) need a `phase_based`
    classification to avoid spurious drift_alerts.

**Broad tier (seeder-driven, rank-based).**
- `seed_broad_tier.py` reads
  `pricempire_item_metadata` for rank-DESC and picks top-N filtered
  by deep-set + `broad_tier_exclusions:`. Idempotent under stable
  inputs.
- A broad item dropped from the seeded output (rank fell out of
  top-N, or operator added it to exclusions) is removed from the
  YAML's `items:` block. The DB row is **preserved** — it becomes
  an orphan. **Once §4.D3's orphan-filter gap is closed (or
  while it remains open), each drop adds to the orphan population
  that `item_unavailability_streak` accumulates rows against
  forever**; see Consequences for the projected scale once broad
  is populated.
- A broad item re-introduced by the seeder (rank climbed back, or
  exclusion removed) is `ON CONFLICT DO NOTHING`-upserted into
  items; existing historical rows in `prices` /
  `pricempire_observations` / `observation_log` / `insights` are
  trivially reused.
- The seeder's report distinguishes `added` (new to items table) vs
  `re_added` (exists in items, returning to YAML) so the operator
  sees re-promotions explicitly.

**Substrate handling (data-preservation invariant).**
- An item is "substrate" iff `market_hash_name` exists in `items`
  but not in the current `data/watchlist.yaml`. There is no
  substrate flag in the DB; the tier label is computed at read
  time from the YAML diff against the items table. (Pre-Phase-2c
  this state was named "orphan"; same semantics, new label.)
- Substrate rows are NOT cleaned up by any current migration or
  script.
  Their rows in `prices`, `pricempire_observations`, `observation_log`,
  `pricempire_observation_log`, `pricempire_item_metadata`, and
  `insights` remain queryable.
- Collectors do NOT poll orphans:
  - `collectors/scheduler.py:_load_watchlist` reads the YAML, not
    the items table, so orphan rows are silently dropped from the
    per-cycle poll list.
  - Pricempire's `collect_snapshot` reads the items table directly
    (preserving orphan refresh via the bulk call) — but the dedup
    gate suppresses identical re-writes, so orphans on a stable
    Pricempire response add zero rows.
- The bot's orphan envelope copy points to the sibling curated-tier
  wear when one exists (e.g. "USP-S | Neo-Noir (Factory New)" is
  orphan; "USP-S | Neo-Noir (Field-Tested)" is the active deep wear
  → bot suggests the active wear).

**Rejected alternative: orphan-cleanup migration.** Would delete
historical data for items the operator might later re-curate.
Preservation is cheap (rows are small); reversal-by-history-loss is
not.

**Rejected alternative: explicit `tier: orphan` in YAML.** Would
require an operator-visible state for items they've already removed
from the YAML, which defeats the point of removal. The
"present-in-items-absent-from-YAML" diff is the orphan state by
construction.

## Consequences

- **Pro:** Tier is a single field in one file. Operator can read
  `git log data/watchlist.yaml` to see every tier change with
  context.
- **Pro:** The YAML loader's `_SUPPORTED_SCHEMA_VERSION = 3` fail-
  fasts on missing tier fields, so partial migrations can't ship
  silently.
- **Pro:** `Tier = Literal["curated", "featured", "substrate"]` is a closed
  set; the type system enforces exhaustiveness at every consumer
  (`bot/tools.py:_attach_tier_envelope`, `api/routes/drift.py`,
  etc.).
- **Pro:** Orphan preservation makes "re-add a previously curated
  item" a one-line YAML edit; no data migration, no insert.
- **Pro:** The Doppler re-introduction (proposal exclusion →
  shipped via `phase_based` classifier) is a working precedent for
  "the classifier subsumes the original reason for an
  exclusion" — useful template for future taxonomy-noise items.
- **Pro (Phase 2c update):** Featured tier is populated to 500
  items in `data/watchlist.yaml` (initially via yesterday's Path
  B v1 bootstrap; the YAML composition is preserved across the
  Path B v1 → Path A switch). Loader / scheduler / API / bot
  featured-tier code paths are exercised in production. The
  test-coverage-only situation flagged as a con in the original
  draft is closed.
- **Pro (Phase 2c update):** Bootstrap chicken-and-egg (§3
  Addendum) is closed by Path A — once commit 2 lands, the
  items table is the catalog substrate (~5,000 rows) and the
  YAML-tier-membership-vs-items-table chicken-and-egg gap that
  motivated path A/B/C deliberation evaporates. Until commit 2
  lands, the items table still reflects yesterday's Path B v1
  bootstrap output (545 rows); commit 2 grows it to ~5,000.
- **Con:** YAML-as-source-of-truth means a service restart is
  required after every tier change. The cost is real but matches
  the rest of the project's YAML reload discipline (ADR 012 §7,
  ADR 022 §2.6).
- **Con (operationally subtle):** "Orphan" is computed at read
  time, so an item's tier can change without any DB write — just
  by editing the YAML. Investigators reading the DB in isolation
  see no signal that an item is orphan; they need to cross-
  reference the YAML. The ARCHITECTURE.md tone-and-style guidance
  ("state in one sentence what the app is supposed to do") applies:
  if you're debugging a "why isn't this polled" symptom, the first
  question is "is it in the YAML?", not "is it in the DB?".
- **Con (known operational debt — unbounded orphan growth in
  `item_unavailability_streak`).** §4.D3's table flags the
  load-bearing gap: the unavailability-streak job has no tier
  filter and writes one row per (orphan × source) per cycle
  forever. Today's 28 orphans produce ~2.6M rows/year (§4.5
  measured baseline). The interaction with §4.D5's broad-to-orphan
  flow makes this worse: once the broad tier is populated (Phase
  2c) and the seeder runs quarterly with ~5-10% rank churn, each
  refresh drops 25-50 broad items into orphan state, and each new
  orphan adds ~93k rows/year to the unavailability_streak counter
  set. Two quarterly cycles past featured-tier launch and the orphan
  population — and the row-rate — could double. The fix is a
  per-job tier filter (§4.D3-TODO); the cost of NOT fixing is row
  count, not correctness. Phase 2c should treat the streak filter
  as adjacent-scope work even if not formally bundled with
  bootstrap.
- **Failure mode (YAML corruption):** A malformed YAML (missing
  schema_version, invalid tier value, missing market_hash_name)
  fails fast at collector / api / analytics startup with a
  pointed ValueError naming the path and the offending row. No
  silent partial behavior. Pinned by the loader's contract
  documented in `scripts/seed_watchlist.py:load_watchlist`.

## Open follow-ups

- **~~Bootstrap design (path A vs B vs C-permanent).~~** ✓ Phase 2c
  selected Path A (§3 Addendum). Commit 1 (this commit) landed the
  rename groundwork; commit 2 (handoff prompt at
  `notes/path-a-bulk-seed-session-prompt.md`) executes the bulk-
  seed. Path B v1 was the working prior approach (yesterday's
  session) and is preserved as audit history in §3 Addendum's
  "Path B variants are rejected" framing. Path B v2 remains an
  unexercised future-revisit option; not pre-committed.
- **Slug algorithm v2 (ADR 005 v2 follow-up).** The Phase 2c
  bootstrap surfaced exactly the collision class ADR 005's
  Consequences section anticipated: two distinct
  market_hash_names (`Sunset Storm 壱` / `Sunset Storm 弐` Factory
  New) collapsing to one slug because `slugify` strips non-ASCII.
  Interim fix is two entries in `broad_tier_exclusions:`. The
  strategic fix is a v2 algorithm that disambiguates non-ASCII
  characters (Japanese ranks, future Cyrillic / Arabic /
  emoji item names from third-party content). ADR 005 specifies
  the workflow: version bump + regeneration migration touching
  every items row, then re-evaluate the exclusion entries.
- **Tier 4 (Steam-only canaries).** Proposal §"Selection criteria"
  deferred this pending characterization of the Steam collector's
  behavior on non-skin item types (stickers, music kits, patches).
  Out of scope for two-tier; lives in the Tier 4 backlog.
- **Per-featured-tier insight types when broad has items.** §4.D3's
  rule is per-insight today; revisit when the broad tier has rows
  whether per-source moving averages on Pricempire-only data
  produce signal worth surfacing in the bot. May want a separate
  rendering channel ("Pricempire-only view") vs. the current
  uniform price-per-source rendering.
- **§4.D3-TODO — analytics-side orphan tier filter.** Five insight
  types currently write rows for orphan items
  (`cross_source_spread`, `cross_source_view`,
  `cross_source_divergence` derived, `moving_avg_7d` /
  `moving_avg_30d`, `item_unavailability_streak`). Four are
  bounded; `item_unavailability_streak` is unbounded and
  load-bearing. Per-job filter pass needed. Streak-reset semantic
  is the design question — does demotion to orphan reset the
  counter to zero, freeze at last-seen, or stop incrementing?
  Each has downstream consequences for the bot's
  `query_unavailability_streak` rendering.
- **Orphan-cleanup tooling (operator-triggered, never automatic).**
  Long-tail orphans accumulating may eventually be worth a
  `scripts/prune_orphans.py` that deletes items + their historical
  rows. Out of scope today; the cost of preservation is small.
- **DMarket alias map (`dmarket_alias:` field) doc.** The
  `seed_watchlist.py:load_watchlist` validates the field's shape
  and warns on featured-tier use. ADR 012 (or a follow-up) should
  cover the alias-resolution path in the DMarket collector itself.
