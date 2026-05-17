# skin-market — open triage items

Non-blocking observations filed for later triage. Not Phase-specific work;
each entry should be closed by a focused commit or migration with a clear
owner/date stamp when picked up.

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
