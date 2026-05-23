# skin-market — open triage items

Non-blocking observations filed for later triage. Not Phase-specific work;
each entry should be closed by a focused commit or migration with a clear
owner/date stamp when picked up.

---

## External architectural review feedback (2026-05)

**Filed:** 2026-05-18. **Status:** documented; per-item action deferred.

Four items surfaced in an external architectural review. Each is
filed here as future-revisit reference; none is in-scope for current
Phase 2c work.

1. **Query-cost DB logging for abuse-potential analysis.** Defer
   until public/multi-tenant access. Current single-bot single-
   operator setup has no abuse surface; instrumentation would
   produce noise without a consumer.
2. **Redis caching layer.** Defer until a measurable latency or
   load problem appears. Postgres + dedup-on-write is sufficient
   at current scale; adding Redis ahead of need is unjustified
   complexity. Revisit when the bot's /price-path p95 latency
   exceeds operator tolerance, or when concurrent-user count
   actually grows.
3. **Tier-filtering location consolidation (architectural debt).**
   Tier filtering currently lives in `collectors/scheduler.py`
   (`_CURATED_ONLY_SOURCES` + `_load_watchlist`), `analytics/drift.py`
   (`curated_set` filter inside `compute_and_store`),
   `analytics/anomaly_detection.py` (implicit via source-pair
   filtering), and the API tier-aware response shapers
   (`api/watchlist_tiers.py:get_tier`). A future ADR should map
   where tier-filtering happens by component and consolidate the
   places that can drift. Real debt; revisit when adding tier
   awareness to another component would otherwise add a 5th
   independent implementation.
4. **Synchronous / blocking I/O patterns in bot tools + analytics
   jobs.** Defer; scale-driven concern not currently active. The
   bot is single-event-loop today; analytics jobs are batch-mode
   under APScheduler. Revisit when concurrent-user count grows or
   when an analytics job's wall-clock starts impinging on the
   30-minute drift cycle.

---

## 7-day Pricempire-skinport cadence characterization

**Filed:** 2026-05-17 (ADR 022 §2.5 follow-up).
**Status:** deferred until the 7-day window matures. Rechecked
2026-05-23 after Path A: `pricempire_skinport` observation history
spanned 2026-05-16T19:33:55Z → 2026-05-23T04:35:39Z (6 days,
9 hours, 1 minute), short of the required 7-day characterization
window. Earliest safe pickup is ~2026-05-24 UTC, subject to another
span check.

ADR 022 §"Open follow-ups" requires a 7-day characterization of the
Pricempire `pricempire_skinport` upstream refresh cadence to convert
the interim `STALE_PRICEMPIRE_MINUTES = 75.0` into a data-driven
permanent value. Started 2026-05-17 with the deploy of `75`. Due
~2026-05-24.

When the window closes:
- Query `pricempire_observation_log` aged distribution for the
  `pricempire_skinport` source over the 7-day window. Compute
  jitter distribution (median, p95, p99), time-of-day dependence,
  per-sub-provider variance.
- Compare against current `STALE_PRICEMPIRE_MINUTES = 75.0`.
- File a follow-up ADR adjusting the constant if the empirical
  envelope warrants a change. Per ADR 022 §2.5: raise if median +
  p95 jitter exceeds 75; lower if well under 75; consider
  per-sub-provider thresholds if time-of-day or per-provider
  variance dominates.

---

## Slug algorithm v2 (ADR 005 v2 follow-up)

**Filed:** 2026-05-18 (Phase 2c bootstrap surfaced the first slug-v1
collision). **Status:** session prompt drafted at
`notes/slug-v2-session-prompt.md` (gitignored — see ADR 024 repo-
hygiene rule); fresh session pending. **The session prompt is a
convenience; this TODO entry is authoritative — if `notes/` is
ever lost, recreate the prompt from this entry.**

### Problem

ADR 005's slug-v1 algorithm (`db/naming.py:slugify`) strips non-ASCII
characters at step 5 (`_NON_SLUG_CHAR.sub("", s)`). At the Phase 2c
bootstrap, two real CS2 items produced the same slug:
- `Desert Eagle | Sunset Storm 壱 (Factory New)` (rank ~379)
- `Desert Eagle | Sunset Storm 弐 (Factory New)` (rank ~405)
Both slugify to `desert-eagle-sunset-storm-factory-new`. The items
table's `slug` UNIQUE constraint correctly rejected the second
INSERT with `psycopg.errors.UniqueViolation`. ADR 005 §"Consequences"
explicitly anticipated this collision class.

**Interim fix in production:** both colliding items added to
`featured_tier_exclusions:` in `data/watchlist.yaml`.

### Scope of slug v2 work (reconstructible from this entry)

- **Three design decisions to pick (advisor pause-point 1):**
  - Transliteration approach: `unidecode` library (recommended;
    standard for non-ASCII transliteration; covers Cyrillic, Greek,
    Arabic, etc.), curated codepoint map (brittle), or codepoint-
    suffix fallback (ugly slugs but zero collision risk).
  - Pre-COMMIT uniqueness check: algorithmic (compute v2 slug for
    every items + metas row, assert no duplicates), schema-level
    (rely on UNIQUE constraint at migration apply), or both.
  - Regeneration migration shape: inline slug-v2 algorithm in the
    Alembic migration file (recommended; self-contained; doesn't
    break when slug v3 ships), vs. import-from-current-`db.naming`.
- **Implementation steps (after design sign-off):**
  - ADR 005 v2 amendment.
  - `db/naming.py:slugify` v2 + `SLUG_ALGORITHM_VERSION = 2`.
  - Pre-commit uniqueness check (script or test).
  - Alembic migration regenerating every `items.slug`.
  - Remove Sunset Storm entries from `featured_tier_exclusions:`.
  - Regression tests pinning v2 behavior on Sunset Storm pair +
    confirming ASCII items produce identical output to v1.
- **Constraints:**
  - Slugs stay RFC-3986 unreserved-only `[a-z0-9-]`.
  - Identical output to v1 for ASCII-only names; only non-ASCII
    items produce different v2 slugs.
  - `bot/tools.py:_find_active_wear` uses slug-based sibling match;
    verify post-migration.
  - The two Sunset Storm items are currently NOT in `items` (the
    Path A bulk-seed in commit 2 will attempt them; slug v1 fails
    before slug v2 lands, so the exclusion stays through commit 2).
- **Pause-points (4):** design decisions; implementation plan;
  post-migration with uniqueness check passing; pre-push final review.
- **Workflow:** standard ARCHITECTURE.md rules — no magic libraries,
  tests for non-trivial logic, advisor coordinates before design
  decisions, operator commits per phase and pushes manually.

Timing: planned for after Path A bulk-seed (commit 2) lands and
stabilizes for 24h on the larger items table, where the collision
surface scales.

---

## Phase 2c Path A bulk-seed (commit 2 pending)

**Filed:** 2026-05-18. **Status:** session prompt drafted at
`notes/path-a-bulk-seed-session-prompt.md` (gitignored); fresh
session pending. **The session prompt is a convenience; this TODO
entry is authoritative — if `notes/` is ever lost, recreate the
prompt from this entry.**

### Selected path

Phase 2c selected **Path A** (bulk-seed items table from Pricempire's
metas catalog) over Path B v1 (fresh HTTP per seeder run; the
working prior approach in yesterday's session). Rationale: forward-
looking storage substrate for a post-v1 on-demand fetch + auto-
promotion feature (Phase 3+ scope; that ADR lands separately).
Path A's bulk-pre-seed means existence queries against `items`
return a row for any catalog item, making the future on-demand flow
a fetch-and-update rather than an existence-create-fetch three-
step. **This commit (commit 2) only does the storage switch; the
on-demand feature is out of scope.**

### Scope of commit 2 work (reconstructible from this entry)

- Write `scripts/seed_catalog.py`:
  - Top 5,000 by Pricempire rank from `/v4/paid/items/metas`.
  - Single transaction (all-or-nothing for ~5,000 INSERTs; NOT
    batched commits). Postgres handles this comfortably; partial-
    state-on-error rollback is the safety property.
  - Fail-fast in dry-run on slug collisions (collect all, present
    as a block, exit without writing).
  - Honor `featured_tier_exclusions:` from YAML.
  - Populate parse-derivable metadata fields by mirroring
    `scripts/seed_watchlist.py`'s parsing exactly: `is_stattrak` +
    `is_souvenir` from name prefixes, `slug` via `db.naming.slugify`,
    `display_name` = `market_hash_name`. Steam-side taxonomy fields
    (`item_type`, `weapon_name`, `skin_name`, `wear`) stay NULL
    when not deterministic.
  - Idempotent via `ON CONFLICT (market_hash_name) DO NOTHING`.
  - CLI: `--dry-run`, `--limit N` (for testing).
- Write `tests/test_seed_catalog.py`.
- Dry-run, capture collision report.
- Decision rule for slug collisions: under ~10 collisions → add
  to exclusions and proceed; dozens → pause for slug v2 first.
- Bulk-seed for real (items table 545 → ~5,000).
- Re-run `seed_featured_tier.py` (DB-source default path; the
  rolled-back `--source=pricempire` flag is not part of Path A
  steady-state) to verify zero diff.
- Restart api / collector / analytics services.
- Verify canaries:
  - Zero `drift_verdict` rows for any non-curated item.
  - Curated 42 items continue at 84 rows × 42 distinct items per
    cycle (drift detector deep-set filter intact at the larger
    items population).
  - §4 wear-swap canary: 2 substrate items continue zero rows.
  - §4.5 substrate obs_log canary: zero advancements on
    `steam_market` / `skinport` / `dmarket` for substrate items.
  - `cross_source_spread` / `moving_avg` row volume on substrate:
    quantify + report. At ~4,400 substrate items, may warrant the
    §4.D3-TODO orphan-filter follow-up (separate commit).
- Rewrite ADR 024 §3 Addendum to reflect Path A landed (not just
  selected). Path B v1 stays in rejected-alternatives.
- Update §4.D5 substrate-set scale acknowledgment (~4,400 items
  post-Path-A; dominated by never-curated items) + classifier-veto
  sentence (`analytics/pattern_classifier.py` fail-fasts on
  pattern_sensitivity entries for non-curated items, by ADR 024
  §4.D2; this protects editorially-classified items from
  auto-promotion to non-curated tiers in any future flow).
- Update Consequences honestly: bootstrap chicken-and-egg
  architecturally resolved by Path A's substrate-as-default;
  storage cost (~5-10 GB/year compressed on
  `pricempire_observations`) documented.

### Pause-points (3)

1. After `seed_catalog.py` is written + tests pass, before dry-run.
2. After dry-run, before deciding what to do with collisions.
3. After bulk-seed + canaries verified, before commit.

### Prerequisites

- Commit 1 (this commit) pushed.
- Canaries hold for 24h on items table at 545 rows (post-
  yesterday's Path B v1 bootstrap).
- Advisor sign-off on commit 1.

---

## Strengthen TestYamlToCuratedSetIntegration second test (or remove)

**Filed:** 2026-05-18 (Phase 2c rename verification — flagged at
sign-off). **Status:** closed by deletion (2026-05-23). The first
test is the real regression pin; the second smoke test returned
`0` in both the bug state and the no-match state, so it was removed
rather than strengthened.

`tests/test_drift.py::TestYamlToCuratedSetIntegration` has two
methods:

1. `test_curated_set_built_from_v3_yaml_uses_curated_literal` —
   the real regression pin. Loads a v3 YAML and asserts the
   `if it.get("tier") == "curated"` set-construction picks up the
   right rows. A regression to the pre-Phase-2c `"deep"` literal
   would produce an empty set here and fail loudly. Keep.
2. `test_compute_and_store_yaml_path_picks_up_curated_items` —
   honestly only a smoke test, not a regression pin. The function
   returns `rows_written = 0` in both the bug state (empty
   curated_set, no iteration) AND the no-match state (curated_set
   populated, items-table lookup returns None). Can't distinguish
   the bug class it's framed to catch.

### Resolution

Removed `test_compute_and_store_yaml_path_picks_up_curated_items`.
The first method remains and carries the regression-pin load by
asserting the exact YAML → `curated_set` construction. The deleted
method was a smoke test with ambiguous value-add because it returned
`0` under both success and regression conditions.

---

## item_unavailability_streak removal (Phase 2c, 2026-05-18)

**Status:** closed by deletion. This entry exists so future-me doesn't
re-introduce the signal for the wrong reason.

### What was removed

- `analytics/unavailability_streak.py` (module)
- `analytics/scheduler.py` hourly hook for the streak compute
- `bot/tools.py` insight-dispatch branch + per-source "unavailable"
  rendering state — sources without observation now uniformly render
  as `never_observed`
- `tests/test_analytics.py::TestUnavailabilityStreak` + the
  `_upsert_observation_log` helper used only by it
- `tests/test_bot.py::test_fresh_unavailable_never_observed` —
  rewritten as `test_fresh_and_never_observed` reflecting the
  two-state model

### Why

The streak counter's original purpose (ADR 015 §4) was rate-limit
detection on free direct-poll upstreams: count consecutive cycles
where Steam / Skinport / DMarket failed to return a row for an item
so the bot could surface "we've been unable to observe this item for
N cycles." With Pricempire's paid API in place as the breadth layer
(ADR 018), that signal stopped being useful — the rate-limit-recovery
diagnostic doesn't usefully drive the bot's user-facing rendering
once Pricempire fills in catalog coverage for the same items.

The signal also produced unbounded-growth orphan rows (6,328 rows /
21h per `docs/phase2b-validation.md §4.5`, projected ~2.6M
rows/year), which surfaced as the "load-bearing operational debt"
con in ADR 024's earlier revision. **The right fix was deletion,
not a tier filter.** A tier filter would have stopped the orphan
growth but kept a signal whose use case had evaporated.

### Do NOT reintroduce because

- "It would give us a tidier per-source freshness signal." Use
  `observation_log.last_observed_at` directly — it's the substrate
  the streak compute was reading anyway.
- "It would help debug a Steam outage." A Steam outage shows up as
  a `stale` state on the bot's price rendering (4h threshold). Add
  collector-side metrics if needed — don't re-derive an insight
  type just to surface the same fact.
- "We need three states (fresh / unavailable / never_observed)."
  Two states cover the user-facing path adequately. The historical
  "unavailable" state was an operator-facing diagnostic; operator
  tooling lives in `docs/operations.md` / metrics, not in insight
  rows.

### What was NOT removed

- The `observation_log` table itself (ADR 017's split is load-
  bearing for drift detection's freshness gate per ADR 022).
- Pre-existing `item_unavailability_streak` rows in the production
  `insights` table — they remain queryable for historical context.
  A future cleanup commit can `DELETE FROM insights WHERE
  insight_type = 'item_unavailability_streak'` once nobody's
  reading them.

---

## Sources-table cadence drift vs. migration 0003

**Filed:** 2026-05-17 (during Phase 2b Step 2).
**Severity:** low. Collectors still run; cycle counts are stable; user-facing
behavior is correct. The drift is operational, not functional.

### Observation

Live DB `sources` table on the dev/test compose stack as of 2026-05-17:

| name | interval_minutes | per_item_delay_seconds | Expected (migration 0003) |
|---|---|---|---|
| steam_market | 30 | 5 | 60 / 5 |
| skinport | 30 | 5 | 15 / 0 |
| dmarket | 30 | 5 | 15 / 3 |

Cadences match the server-default values (`server_default=sa.text("30")` /
`server_default=sa.text("5")`), not the migration 0003 backfill values.

### Hypotheses

1. Migration 0003 ran when the sources table held only some of these
   rows; later-added rows picked up the column defaults and never got
   the name-targeted UPDATE retroactively. Plausible but doesn't fully
   explain why all three curated rows ended up at 30/5 — at least
   `steam_market` and `skinport` were in the table at 0003's time, so
   their values should have been backfilled.

2. `scripts/seed_watchlist.py` overwrites cadence values on every run.
   If the seed script writes `interval_minutes=30, per_item_delay=5`
   for every source (or unsets them, falling back to server defaults),
   a re-run after 0003 would wipe the backfill. **Worth checking
   first** — if true, this is the actual bug, not the migration. The
   fix would be to (a) make the seed script preserve existing cadence
   values, or (b) backfill cadence into the seed script's own writes
   keyed by source name.

3. A different operator-run script (or manual psql) reset the sources
   table at some point and the 0003 backfill values weren't replayed.

### Side-evidence

Steam cycles in the live collector log are NOT firing at a consistent
30-min interval either (observed starts at 19:21, 19:27, 19:38 on
2026-05-16). Possibly related to the all-zero "0 written, 0 unchanged,
0 unavailable" pattern on those cycles — Steam may be in a degraded
state. Worth correlating with `_apply_pause` events in the rate-limit
ladder when picking this up.

### Triage approach (when picked up)

1. Inspect `scripts/seed_watchlist.py` for cadence-overwriting behavior.
   If present, that's the load-bearing bug — fix the seed script first.
2. Manually `UPDATE sources SET interval_minutes = ..., per_item_delay_seconds = ...
   WHERE name IN (...)` to restore migration 0003's intended values, OR
   write migration 0011 that re-applies the backfill defensively (idempotent).
3. Verify cycle log shows 60/15/15 cadence after fix.
4. Investigate the Steam all-zero-cycle pattern separately.

### Not affected by this drift

Phase 2b's Step 2 migrations (0008/0009/0010), the drift detector, the
Pricempire observation log, and the compression policies are all
independent of the cadence values — they key off table names and
foreign keys, not interval timing. The two-tier rate-limit math
(deep-tier-only for Steam/DMarket because the alternative would
exceed cycle budget) is also independent of the specific interval
values; the math justifies the *direction* of the split regardless
of whether Steam runs at 30 or 60 min.

---

## Destructive migration test isolation

**Filed:** 2026-05-17 (during Phase 2b Step 2).
**Severity:** medium. The test does what its docstring advertises and
the destructive marker is on by default, but the architectural answer
is that destructive tests shouldn't be able to vaporize a live dev DB
just because someone passed `-m destructive` from the wrong cwd.

### Observation

`tests/test_migration_roundtrip.py::test_migration_roundtrip_then_seed`
runs `alembic downgrade base` → `alembic upgrade head` against
whichever Postgres `DATABASE_URL` points to. On the DGX Spark, that
is the same Postgres instance that the collectors / API / bot service
in production read and write to. Running `pytest -m destructive`
wiped ~24h of accumulated Phase 2a `pricempire_observations` data
plus the freshly-backfilled 281 `pricempire_observation_log` rows on
2026-05-17 during Step 2 verification.

### Triage approach (when picked up)

Pick one of three reasonable answers — whichever is simplest at
implementation time:

1. **Test database isolation.** Require `DATABASE_URL` to end in
   `_test` (or some other naming convention) for destructive tests to
   proceed; skip with an explicit message otherwise. Operator runs
   tests by pointing `DATABASE_URL` at a throwaway DB.

2. **`--allow-prod-db` flag.** Destructive tests refuse to run unless
   pytest is invoked with `--allow-prod-db`. Belt-and-braces for the
   case where the wrong env is loaded.

3. **Snapshot-and-restore.** Before the destructive test runs, take a
   `pg_dump` of the Pricempire tables (and `prices`, `insights`) into
   a tempfile; restore on test exit (success or failure via a finalizer).
   Self-contained, but tied to Postgres tooling.

Recommend (1) — simplest, fail-safe by default, matches the existing
`_db_required` skip pattern in the codebase.

### Not affected — Phase 2b moves forward

Data loss accepted; system self-recovers on existing cycle cadence.
Step 5's drift detector will run with shallower-than-steady-state
history for ~24-48h post-recovery; the Step 5 validation breakdown
should note this caveat so the numbers aren't read as steady-state.

---

## Destructive test confirmation gate

**Filed:** 2026-05-17 (during Phase 2b Step 2).
**Severity:** low. Belt-and-braces atop the test-isolation item above.

### Observation

Even with database-name isolation in place, a destructive `pytest -m
destructive` run should not vaporize the targeted database without
some form of typed user confirmation. The current behavior is
"`pytest -m destructive` and Postgres is dropped" — there is no
"are you sure?" gate.

### Triage approach (when picked up)

A conftest-level fixture or `pytest_collection_modifyitems` hook that,
when destructive tests are about to run, prints the targeted
`DATABASE_URL` host/dbname and waits for the user to type the dbname
back. Matches the convention `kubectl drain` or `terraform destroy`
use. Skipped automatically in CI when `stdin` isn't a tty (CI runs
should rely on the isolation in the first item above; the
confirmation gate is for interactive operator runs).

### Pairs with

The test-isolation item above. Implementing isolation first means the
confirmation gate is rarely-load-bearing; implementing the gate first
means isolation is less critical. Do isolation first.

---

## Drop the dead `session` parameter on `_load_watchlist`

**Filed:** 2026-05-17 (during Phase 2b Step 7.1.5).
**Severity:** low. Cosmetic / API hygiene; no functional issue.

### Observation

`collectors/scheduler.py::_load_watchlist` accepts a `session: Session`
parameter that the YAML-driven implementation no longer uses (the
function reads `data/watchlist.yaml` directly, never touches the DB).
The body explicitly `del session`s the unused arg to silence the
linter. The parameter is retained for signature stability across the
Phase 2b rollout: the existing `_run_cycle` call site already opens
a Session for other reasons and passes it through; dropping the
parameter would touch that call site and the test_scheduler test
that builds a Session as the first positional arg.

### Triage approach (when picked up)

During a future scheduler-cleanup pass (or any refactor that already
touches `_run_cycle`), drop the parameter:

1. Remove `session` from the signature; remove the `del session`.
2. Update the call site in `_run_cycle` — the Session is still needed
   for other operations there, just not for `_load_watchlist`.
3. Update the test file's `_lw(session, source_name=...)` invocations
   and the new `TestLoadWatchlistTierFilter` tests that pass `None`
   as the session arg.

Each change is one line. Skip if there's no compelling reason to
touch the scheduler — the dead parameter is harmless.

### Not affected — Phase 2b moves forward

Step 7.1.5 ships the parameter intact. Future Phase 2c+ work can
clear it during any scheduler-adjacent refactor.

---

## test_writes_top_n_broad_tier_entries — sentinel-rank brittleness

**Filed:** 2026-05-17 (during Phase 2b Step 3).
**Severity:** low. The test currently passes via a "widen target_size
to 100" workaround; the underlying brittleness remains.

### Observation

`tests/test_seed_broad_tier.py::test_writes_top_n_broad_tier_entries`
uses sentinel ranks 1-10. Production items in `pricempire_item_metadata`
also occupy that range (★ Butterfly Knife sits at rank 5 today,
★ Sport Gloves Hedge Maze at rank 8). Rank collisions are resolved by
UUID tie-break, which is data-dependent and not stable across runs.

Initial implementation asserted "sentinels are the top-5 broad items
at target_size=5" — failed because Butterfly Knife tied rank 5 with a
sentinel and won the tie. Current workaround: target_size=100 with
subset-assertion ("all sentinels appear somewhere in broad tier").
Works today; ages badly as production data grows.

### Triage approach (when picked up)

Once destructive-test isolation lands (separate TODO above), the test
can run against a dedicated test DB where production rank collisions
don't exist. At that point, two options:

1. Move sentinel ranks to obviously-fake values (e.g. 99001-99010).
   Real items can't have ranks that high (Pricempire's catalog tops out
   around ~91k items). Sentinels are guaranteed to lose to any real
   item in the top-N cut; the assertion shifts to "sentinels appear if
   and only if target_size > production_count + 1." Brittle in a
   different way (depends on production_count being stable).

2. Insert exclusively-sentinel data (delete production metadata in the
   fixture). Clean isolation. Requires the test-isolation work first
   so that "deleting production data" is safe.

Option 2 is the right shape once isolation is in place. Option 1 is
the duct-tape workaround if isolation is delayed.

### Not affected — Phase 2b moves forward

The current target_size=100 workaround works against production data
state as of 2026-05-17. If a future production cycle pushes 100 items
above sentinel rank 10, the test will start failing; that's the cue
to pick up this TODO.

---

## 7-day Pricempire cadence characterization (ADR 022 §2.5 follow-up)

**Filed:** 2026-05-17 (paired with ADR 022 commit 5594bee).
**Severity:** medium. The `STALE_PRICEMPIRE_MINUTES = 75` value
shipped in ADR 022 §2.5 is an **interim** choice based on a 21h 30min
validation window. Leaving it permanent by default — the failure mode
the explicit end-state framing was meant to prevent — would freeze a
threshold that may be too tight, too loose, or in the wrong
per-sub-provider shape entirely.

**Target date:** **2026-05-25** (assumes the analytics rebuild that
takes the 75-min threshold live happens on or about 2026-05-17 + 7
full days of post-rebuild data + 1 day buffer for the characterization
work itself). **If the rebuild slips past 2026-05-18, update this
target date accordingly** — the trigger is rebuild + 7d, not a
calendar date.

### Trigger

Operator (or whoever picks this up) confirms the rebuild date by
checking the most recent `analytics` image timestamp:

```bash
docker images --format "{{.Repository}} {{.CreatedAt}}" | grep skin-market-
```

Or by checking the first `drift_verdict` row whose `meta_info->>'threshold_used'`
calculation reflects the new constant (will require a separate marker
in the meta_info or a separate cycle-time anchor — see "Implementation
notes" below).

### Scope

Per ADR 022 §6:

1. **Jitter distribution** per sub-provider over a 7-day window:
   median, p95, p99 of inter-write gaps for
   `pricempire_observation_log.last_observed_at` by `source_id`.
2. **Time-of-day dependence**: bucket the cadence by UTC hour-of-day
   and check whether the upstream refresh slows during certain
   windows (Pricempire's own internal cron pattern, or upstream
   sub-provider downtime windows).
3. **Outage tails**: any single-day outliers (gaps > 6h) — were they
   correlated across sub-providers (= a Pricempire-side outage) or
   isolated to one (= upstream-provider-side outage)?
4. **All six sub-providers**, not just `pricempire_skinport` /
   `pricempire_dmarket` — informs whether to ever expand the
   meaningful-pair set.

### Deliverable

A follow-up ADR (provisionally ADR 026) that:

- Cites the 7-day characterization findings.
- Revises `STALE_PRICEMPIRE_MINUTES` (one of: lower, raise, split
  per-sub-provider, or affirm 75).
- Updates `analytics/drift.py:117` and
  `tests/test_drift.py::test_stale_thresholds_match_adr_022` if the
  value changes.
- Cross-references ADR 022 §2.5 as superseded.

### Implementation notes

The validation queries from `docs/phase2b-validation.md` §1 / §3.a
generalize directly — same shape, just over a wider window. Add a
per-sub-provider grouping to the percentile query in §3.a. No new
schema or instrumentation needed; everything is in
`pricempire_observation_log` already.

### Not affected — system runs fine in the meantime

The 75-min interim is functionally correct for the typical cadence
envelope. The risk this TODO addresses is "we forget to revise" and
75 silently becomes the permanent value, not "the system is broken
in the meantime." If picked up later than 2026-05-25, the only cost
is delayed empirical confirmation, not operational degradation.
