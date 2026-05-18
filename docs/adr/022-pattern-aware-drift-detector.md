# ADR 022 — Pattern-aware drift detector

**Status:** Accepted
**Date:** 2026-05-17
**Related:** ADR 010 (analytics design — divergence-first), ADR 017
(observation_log freshness split), ADR 018 (Pricempire as breadth
source — three timestamps), ADR 021 (pattern-sensitivity classifier),
ADR 023 (pricempire_observation_log), ADR 024 (two-tier watchlist).

## Context

Pricempire ingest (ADR 018) gives the project two independent prices
per curated-tier item per cross-marketplace pair: the direct collector
(Skinport, DMarket) and Pricempire's view of the same provider. With
those streams flowing, there's a question they're now positioned to
answer: when do they disagree?

`cross_source_divergence` (ADR 010) already handles curated × curated
divergence — e.g., a real-money Skinport-vs-DMarket spread that
diverged from its rolling baseline. That's structurally different from
what we need here: the comparison is curated × Pricempire-mirror, and
the right baseline is "do they currently match," not "did the spread
move." A separate detector keeps the two insight types disjoint and
keeps each pipeline's logic readable.

**Load-bearing assumption: this detector inherits the ADR 017
observation_log split.** Two invariants from that ADR are
prerequisites for the verdict semantics in §2 to be coherent:

1. `observation_log.last_observed_at` advances on every successful
   poll attempt, **before** the dedup gate (`collectors/base.py` for
   curated, `collectors/pricempire.py:481-486` for Pricempire-side
   per ADR 023). Without this, a flat-market period silently ages the
   freshness reading and the detector falsely reports stale.
2. `prices.price` reflects the source's actual most-recent value.
   Dedup-on-write suppresses identical re-writes but never alters
   stored values; the latest row always holds the latest real number.

If either invariant breaks in a future change, this detector will
silently produce verdicts that are technically honest but
semantically misleading. ADRs 017 and 023 are the contract these
decisions sit on top of.

## Decisions

### §2.1 Meaningful pairs are direct × Pricempire-mirror only

Two pairs per curated-tier item:

```
(skinport, pricempire_skinport)
(dmarket,  pricempire_dmarket)
```

Steam is excluded because Pricempire doesn't serve Steam pricing (ADR
018 §"Context"). Cross-marketplace pairings (e.g., `skinport ×
pricempire_dmarket`) mix taxonomies and would produce meaningless
spreads. Pinned in `analytics/drift.py:113-116` and tested at
`tests/test_drift.py::test_meaningful_pairs_match_adr_018`.

Per cycle: 42 curated-tier items × 2 pairs = 84 evaluations.

### §2.2 Seven verdict kinds, append-only write pattern

The `insights.insight_type = 'drift_verdict'` row carries the verdict
in `meta_info->>'verdict'`. Seven possible values:

| Verdict | Trigger | `insights.value` |
|---|---|---|
| `drift_alert` | `abs(drift) > effective_threshold` | signed Decimal |
| `no_drift` | `abs(drift) ≤ effective_threshold` | signed Decimal |
| `pattern_skip` | classifier says `phase_based` | NULL |
| `stale_curated` | curated last-polled > `STALE_CURATED_MINUTES` | NULL |
| `stale_pricempire` | Pricempire last-polled > `STALE_PRICEMPIRE_MINUTES` | NULL |
| `stale_both` | both sides exceed their stale thresholds | NULL |
| `no_comparable_data` | one side missing or `pricempire_price = 0` | NULL |

Each cycle appends one row per `(item, pair)`. Re-runs add duplicate
rows; downstream consumers use `DISTINCT ON (item_id, source_a_id,
source_b_id) ORDER BY computed_at DESC` to surface the latest. The
API endpoint (`api/routes/drift.py`) and bot tool (`bot/tools.py`)
both follow this pattern.

### §2.3 Precedence: phase_based → missing data → stale → fresh

`decide_verdict` (`analytics/drift.py:163-310`) evaluates in this
order; first match wins:

1. **`phase_based` wins over staleness.** Pattern-aggregated items
   produce structurally meaningless drift regardless of freshness;
   the verdict is `pattern_skip` even when one side is stale. ADR
   021's classifier is the input.
2. **Missing data → `no_comparable_data`.** Either side null, or
   `pricempire_price == 0` (would div-by-zero).
3. **Stale gate.** Both sides exceeding their thresholds →
   `stale_both`; one side → `stale_curated` or `stale_pricempire`.
4. **Fresh on both sides → drift math.** See §2.4.

### §2.4 Threshold semantics

Baseline: `BASELINE_DRIFT_THRESHOLD = Decimal("0.10")` (10%).
Effective threshold = `baseline × classification.threshold_multiplier`,
quantized as Decimal end-to-end (money discipline; float() of a price
or ratio is a bug).

Drift formula: signed `(curated - pricempire) / pricempire`, quantized
to 4 decimal places (0.01% resolution). Asymmetric formula by design
— Pricempire is the breadth-coverage reference; the question is how
far the curated value has drifted from it.

Comparison is strict-greater on `abs(drift)`:

- `abs(drift) > effective_threshold` → `drift_alert`
- `abs(drift) ≤ effective_threshold` → `no_drift`

Boundary: drift exactly at ±threshold (after 0.0001 quantization)
lands on `no_drift`. The quantization makes the boundary deterministic
across runs.

Sign symmetry: `abs()` means the verdict doesn't distinguish positive
(curated higher) from negative (Pricempire higher) drift. The
underlying signed value is preserved in `insights.value` and meta;
bot rendering can surface direction without the detector enforcing
asymmetric thresholds.

### §2.5 Stale-gate constants — interim revision and end-state

Original constants (Phase 2b foundation, 11aaf25):
- `STALE_CURATED_MINUTES = 30.0`
- `STALE_PRICEMPIRE_MINUTES = 30.0`

Empirical evidence from `docs/phase2b-validation.md §3.a` and §3.1:
the 30-min Pricempire stale gate produces structural every-other-cycle
`stale_pricempire` on the skinport pair, because Pricempire's
upstream `pricempire_skinport` refresh cadence is approximately
60-minute mean with ±30 min jitter (observed range 30-90 min over 44
cycles / 21h 30min). The detector's effective skinport-pair coverage
in the current configuration is ~50% of cycles; one real one-shot
drift event (Hedge Maze, +36%) was caught only because cycle 7
landed in a fresh phase.

**Interim revision: `STALE_PRICEMPIRE_MINUTES = 75.0`.** Chosen to
cover the 90-min worst-case jitter observed in the validation window
with a small safety margin, while staying tight enough that a real
multi-hour Pricempire outage still trips the stale gate (§4).
`STALE_CURATED_MINUTES` stays at 30.0 — curated polling cadence is
15-30 min and that gate is correctly calibrated.

**End-state of the interim.** The interim holds until the 7-day
characterization (§6) completes, at which point a follow-up ADR
revises based on the empirical jitter distribution:
- If median + p95 jitter exceeds 75 → raise further to cover.
- If well under 75 → lower toward p99 + safety margin.
- If the cadence proves time-of-day-dependent or sub-provider-specific
  in ways not captured by a single constant → consider per-sub-provider
  thresholds (currently rejected per §3.alt-A).

Until that follow-up ADR lands, 75 is the operating value.

### §2.6 30-minute detector cadence, gated by `DRIFT_DETECTION_ENABLED`

APScheduler interval job in `analytics/scheduler.py`. The feature
flag is read at process start; flipping `DRIFT_DETECTION_ENABLED`
requires an analytics service **restart** (env-var-only change per
ARCHITECTURE.md's restart-vs-rebuild discipline), not a rebuild.

`compute_and_store` loads `data/pattern_sensitivity.yaml` and
`data/watchlist.yaml` **once per cycle**, treating both as immutable
for the cycle's duration. The curated-tier filter applied to the
watchlist YAML is the regression-canary checked in
`docs/phase2b-validation.md §4 / §4.5`: zero `drift_verdict` rows
must appear for substrate-tier (post-Phase-2c rename of orphan)
items in any future cycle. Mid-cycle YAML
edits don't take effect until the *next* cycle, and an analytics
service restart is the documented refresh path for both YAMLs.

Per-cycle compute: 84 INSERTs, ~1-2s wall time. Compression and
retention policy on the resulting rows: **deferred**. At steady-state
volume (84 rows/cycle × 48 cycles/day × 365 = ~1.47M rows/year on
`insights`), the storage cost is real but not yet operationally
relevant; a future ADR can lift the deferral if growth crowds out
other consumers of `insights`.

## Alternatives considered

**Compute drift on the fly in API/bot.** Rejected: every read of
`/items/{slug}/drift` would do the LEFT JOIN LATERAL + age math +
classifier lookup. Caching to `insights` is much cheaper at the
84-pair-per-cycle volume, and the bot's read path stays a simple
SELECT.

**Store drift as a column on `pricempire_observations`.** Rejected:
drift is a cross-source-pair computation, not a per-observation
property. Would force schema duplication or a join-then-store anti-
pattern that violates the per-row independence of the hypertable.

**Symmetric formula `(curated - pricempire) / midpoint`.** Rejected:
the natural denominator is "what we're comparing against." Pricempire
is the breadth-coverage reference layer (ADR 018); the asymmetric
formula expresses the intended semantic.

**Treat `phase_based` as a multiplier (e.g., 5×) instead of a skip.**
Rejected: ADR 021's classifier addresses cases where the underlying
`market_hash_name` aggregates phases with fundamentally different
prices. A drift number on a pattern-aggregated item is structurally
meaningless; no multiplier rescues that. Pinned by the `decide_verdict`
precedence rule (§2.3).

**(§3.alt-A) Per-pair thresholds.** Rejected: threshold is a
value-level concept (when does a drift ratio represent a divergence?),
not a freshness-level one. The per-pair stale gates are already
separately calibrated; threshold doesn't need to track. If a future
analysis shows that certain pairs systematically need different
thresholds (e.g., dmarket-pair tighter than skinport-pair), this can
be revisited.

**(§3.alt-B) Tiered verdicts (`drift_warning` + `drift_alert`).**
Deferred: a binary verdict suffices for v1. The bot's user-facing
rendering can apply additional thresholding on the signed drift value
in `insights.value` if a "warning band" is wanted later. Splitting
the schema to bake in two thresholds before users have asked for them
is premature.

## Consequences

- **Pro:** Real divergences surface every cycle on the dmarket pair
  (validation doc §3.1: 2 of 42 items in persistent drift state at
  the time of writing).
- **Pro:** `pattern_skip` correctly suppresses the Doppler / Marble
  Fade / Tiger Tooth noise that swamped Phase 2a analysis.
- **Pro:** Append-only writes make rollback trivial — `DELETE FROM
  insights WHERE insight_type = 'drift_verdict'` and the detector
  rebuilds from the next cycle. No state machine, no recovery.
- **Pro:** Per the §2.4 sign-preservation, the `insights.value`
  column carries the signed drift, so bot rendering can show direction
  without the detector enforcing asymmetric thresholds.
- **Con (load-bearing, mitigated):** The skinport pair was structurally
  under-covered at the original `STALE_PRICEMPIRE_MINUTES = 30`. The
  interim revision to 75 mitigates this for the typical jitter range;
  full mitigation requires the 7-day characterization (§6).
- **Con (acknowledged):** A `drift_alert` with stable underlying
  values produces *identical* drift numbers across many cycles
  (validation doc §3.1). This is correct behavior — dedup-on-write
  + ratio-of-stable-values = stable ratio — but consumers should
  understand "drift = 0.1046 for 15 cycles" is one persistent event,
  not 15 independent confirmations. The bot's `query_drift` shaper
  already treats the latest verdict per pair as authoritative; this
  con is operator-facing, not user-facing.
- **Failure mode (Pricempire down).** At `STALE_PRICEMPIRE_MINUTES =
  75`, a Pricempire outage exceeding ~75 minutes flips all 84 (item,
  pair) rows to `stale_pricempire`. `drift_alert` rows stop appearing
  until Pricempire recovers and the next cycle re-evaluates. This is
  correct behavior — we shouldn't be reporting drift signals against
  a stale Pricempire view — and the bot's `query_drift` rendering
  surfaces the staleness verbatim per Step 9's coexistence-rule
  framing. Accepted because the alternative ("fall back to a single
  curated source") would lose the cross-source nature of the
  detector's value.

## Empirical findings

All numbers in this section were **measured before the §2.5 interim
revision took effect** — i.e., with `STALE_PRICEMPIRE_MINUTES = 30`
running in production over the 21h 30min / 44-cycle validation
window (2026-05-16 23:16 UTC → 2026-05-17 20:46 UTC). See
`docs/phase2b-validation.md` for the full SQL and per-cycle data.

**Post-rebuild behavior (with `STALE_PRICEMPIRE_MINUTES = 75`)
will differ.** The two predicted changes:

- `stale_pricempire` rate on the skinport pair should drop from ~50%
  of cycles toward ~0% of cycles under typical Pricempire upstream
  cadence. The 21.2% aggregate `stale_pricempire` figure below was
  almost entirely skinport-pair false-positives; with 75 covering
  the observed 30-90 min jitter envelope, that share should collapse
  to outage-only events.
- `drift_alert` distribution should re-balance away from the current
  ~96% dmarket-pair share, because skinport-pair items will start
  getting fresh evaluations on the cycles where they previously
  reported `stale_pricempire`. The Hedge Maze-shaped one-shot events
  (validation §3.1) are precisely the class that should surface more
  often post-rebuild.

The 7-day characterization in §6 is the trigger for re-measuring
both. Numbers below should be read as "what the detector did in its
first day at the original threshold," not "what readers should
expect now."

- **Cadence cleanliness** (validation §1): 44 cycles, 84 rows each
  cycle exactly, 30-min cadence honored across the window. No
  misfires, no DB errors.
- **Verdict distribution** (validation §2): 54.3% no_drift, 21.2%
  stale_pricempire (artifact of the §2.5 mis-calibration, addressed
  by the interim revision), 11.9% pattern_skip, 11.9%
  no_comparable_data, 0.7% drift_alert. Bimodal Phase-A / Phase-B
  oscillation on the skinport pair locked in from cycle 1; per-item
  variance within a cycle is empirically zero (p10 = p50 = p90 for
  pricempire age).
- **`pattern_skip` is rock-stable** (validation §3): exactly 10
  rows/cycle from cycle 1 (5 phase_based items × 2 pairs). Classifier
  wiring confirmed.
- **Pricempire skinport cadence** (validation §3.a): ~60 min mean
  with ±30 min jitter (observed range 30-90 min). Per-cycle, all 40
  scored items share the same pricempire_skinport age within stddev
  = 0.00 resolution — the upstream refresh is a synchronized batch
  per Pricempire cycle. The 75-min interim threshold in §2.5 is
  sized against this observed envelope.
- **`drift_alert` characterization** (validation §3.1): 26 alert
  rows = 2 persistent dmarket-pair divergences (Buzz Kill FT at
  +10.46%, Sport Gloves Pandora's Box FT at +13.16%) + 1 one-shot
  skinport-pair alert (Hedge Maze FT at +36.08%). 96% dmarket-pair
  share reflects the §2.5 skinport coverage gap; the interim
  revision should narrow this asymmetry over the next 7 days.
- **The two persistent dmarket alerts have identical drift values
  across all alert cycles.** Pre-write investigation confirmed this
  is dedup-on-write + ratio-of-stable-values, not a frozen feed: the
  Buzz Kill DMarket `prices` table has 28 rows over the 21h window
  with dedup correctly suppressing identical re-writes during stable
  periods; `observation_log` is fresh. The §4 "stable real
  divergence" framing is empirically grounded.

## Open follow-ups

- **7-day characterization of per-sub-provider Pricempire upstream
  cadence.** Required to convert the §2.5 interim threshold into a
  data-driven permanent value. Should characterize: jitter
  distribution (median, p95, p99), time-of-day dependence, per-sub-
  provider differences across all six (only `pricempire_skinport`
  and `pricempire_dmarket` are in the meaningful-pair set today, but
  the other four inform future scope).
- **Coexistence rule key-name mismatch.** `bot/tools.py:417-419`
  filters `cross_source_divergence` rows by `meta["source_a_name"]`,
  but `analytics/anomaly_detection.py:182-189` writes only
  `source_a_id` (no name). Today harmless by construction
  (cross_source_spread is curated-only); latent if the spread
  schema ever changes. Belongs in the TODO surface for this ADR
  since the coexistence rule landed alongside the bot integration of
  the detector. Three remediation paths in `docs/phase2b-validation.md
  §7`.
- **Future enhancement: value-age fields in meta_info.** Surface
  `curated_value_age` (= `NOW() - prices.timestamp` for the curated
  side) and `pricempire_value_age` similarly. Would give the bot
  and operator a way to distinguish "stable real divergence" from
  "frozen feed" without requiring a separate analytics path. Out of
  scope for this ADR; filed for a future phase.
- **Compression / retention policy on `drift_verdict` rows.** ~1.47M
  rows/year on `insights` from this detector alone. Not operationally
  urgent today; revisit in a future ADR (likely paired with ADR 025's
  scope expansion) if `insights` table growth crowds out other
  consumers.
