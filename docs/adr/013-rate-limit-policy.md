# ADR 013 — Rate-limit policy

**Status:** Accepted
**Date:** 2026-05-12
**Related:** ADR 006 (collector resilience), ADR 009 (scheduler design), ADR 010 (analytics design), ADR 012 (DMarket collector)

## Context

The day-1 polling cadence (Steam 30 min / 5s, Skinport 5 min, DMarket 15 min / 3s) crossed both upstreams' rate-limit thresholds during the May 2026 build/test/restart cycles. Skinport responded with hard HTTP 429s and a whole-IP ban; Steam responded with two simultaneously co-existing failure modes:

1. **Soft degrade**, observed first: `success:true` with empty price fields for items that normally have listings. 27/48 then 48/48 of a cycle's items came back empty in three hours.
2. **Hard 429**, observed later (12 May, ~20:44 UTC): full IP-level throttling, body literally `null`, no `Retry-After` header, even for manifestly-fake item names — confirming the 429 fires before any item-existence check.

Two failure modes, one root cause: polling too aggressively. The collectors needed to (a) read polling cadence from a single source of truth that the operator can flip, (b) honor `Retry-After` when sources send it and back off sensibly when they don't, and (c) distinguish "this item has no listings" (real signal — the bot will eventually answer "is X available on Y?" from this) from "the source declined to answer" (noise the bot must filter).

## Decisions

### 1. Per-source policy as scalar columns on `sources`

Two new columns:

- `interval_minutes` (INTEGER, NOT NULL, server_default=30)
- `per_item_delay_seconds` (INTEGER, NOT NULL, server_default=5)

`build_scheduler()` queries `sources WHERE enabled = TRUE` at startup and reads these columns to register one APScheduler job per enabled source. The DB flag is the single switch: `enabled = FALSE` removes the source from the scheduler, matching the analytics layer's existing `WHERE enabled = TRUE` behavior. No more analytics-vs-collector asymmetry around what "this source is on/off" means.

Initial values set by migration 0003 (the post-degradation policy):

| source         | interval_minutes | per_item_delay_seconds | rationale |
|----------------|------------------|------------------------|-----------|
| `steam_market` | 60               | 5                      | halves request volume relative to 30/5; the degradation event proved we were at threshold |
| `skinport`     | 15               | 0                      | bulk fetch — per-item delay is N/A; cadence conservative post-ban |
| `dmarket`      | 15               | 3                      | unchanged — healthy at this rate |

The migration **leaves `skinport.enabled` as-is**. Re-enabling is an explicit operator step after verifying the ban has cleared (`UPDATE sources SET enabled = TRUE WHERE name = 'skinport'` followed by `docker compose restart collector`).

**Why scalar columns rather than a JSONB policy blob.** Today's tuning knobs are scalar and live naturally next to `enabled`. JSONB would be appropriate when there's something non-scalar to store — structured backoff curves, per-status-code rules, conditional pacing. **When we have non-scalar policy to store, migrate the rate-limit columns to a JSONB column.** Documented here so future readers don't re-derive this decision.

**Why columns rather than code constants.** The operator can adjust pacing without a deploy: `UPDATE sources SET interval_minutes = 90 WHERE name = 'steam_market'` is a one-line live change. Rate-limit thresholds vary with upstream state (post-ban tightening, recovered loosening); code constants would require a redeploy per tune.

**Why detection logic stays in code per-collector.** The failure shapes differ:

- Skinport returns HTTP 429 with optional `Retry-After`.
- DMarket returns HTTP 429 or non-2xx for declines.
- Steam returns `success:true` with empty fields (soft) OR HTTP 429 with `null` body (hard).

Detection is inherently per-source. Tuning knobs are uniform. Columns for knobs, code for detection — clean split.

### 2. Retry-After honored at two layers

Every collector now:

1. **In-call retry layer.** On HTTP 429, parses the `Retry-After` header. If present and parseable as an integer, sleeps min(value, 60s) before the next retry — caps in-call sleep at 60s so a long ban doesn't block the cycle in a single sleep. If absent, falls back to `full_jitter_backoff` (existing behavior, ADR 006 §3).
2. **Job-pause layer.** On retry exhaustion with at least one 429 observed, raises `RateLimited(source_name, retry_after_seconds)`. The scheduler's cycle wrapper catches it and calls `scheduler.modify_job(f"{source_name}_cycle", next_run_time=...)` to defer the next firing of just that source. Other sources keep running.

**Pause-duration ladder** when `Retry-After` is absent:

- Initial pause: 5 minutes.
- If another 429 fires within 1 hour of the last: double (5 → 10 → 20 → 40 → 60 min cap).
- After 1 hour with no 429s: memory ages out, ladder resets to 5 min.

The choice of 5 min initial / 1 hour cap matches the qualitative shape of upstream rate-limit recovery times observed during this incident — Steam's IP cooldown is hours-scale, but we don't want to defer next firing by hours on the first 429 since the cooldown might already be expiring. The doubling ladder converges to that scale within ~4 strikes.

`compute_pause_seconds` is pure (modulo a module-level state dict guarded by a lock) and unit-tested independently of APScheduler.

### 3. `unavailable` vs `declined` observation split

The pre-change `unavailable` counter conflated two semantically distinct outcomes:

1. **Source confirmed the item has no listings.** Genuine signal — the ~10 high-tier rares (Souvenir Dragon Lores, Howl FN, etc.) consistently come back this way. The bot will eventually answer "is X available on Y?" from this; rate-limit noise must not pollute it.
2. **Source declined to answer.** Rate-limit disguise — counts of these should drop as the rate-limit fixes work; the operator needs visibility.

Two outcomes in code now:

- **`DECLINED`** (`collectors.base._DeclinedMarker` sentinel): 4xx non-429, retry exhaustion on timeouts/5xx, bulk fetch error.
- **None** (yielded from `collect_cycle`): ambiguous — `success:true`/`success:false` with no parseable price, or item missing from a bulk response.

The scheduler's `_run_cycle` counts both, and applies a cycle-level heuristic to relabel ambiguous Nones as declined when the cycle as a whole came back outsized-empty.

**Heuristic threshold:** if more than 50% of a cycle's items came back empty (`DECLINED` + None combined), the ambiguous Nones get relabeled as `declined`. Otherwise they count as `unavailable`.

**Why 50%.** Calibrated against the May 2026 Steam event:

| cycle              | empty count | empty fraction | label outcome under heuristic |
|--------------------|-------------|----------------|-------------------------------|
| healthy baseline   | ~10/48      | ~21%           | all `unavailable` (signal preserved) |
| degraded (16:13)   | 27/48       | 56%            | all `declined` (heuristic fires) |
| fully degraded     | 48/48       | 100%           | all `declined` (heuristic fires) |

Threshold lives in `collectors.scheduler.AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD` (= 0.5) — one place to tune if observed baselines drift.

**Why a heuristic, not per-item detection, for Steam.** The 5a investigation could not capture the soft-degrade response shape live — Steam was in a hard-429 ban when the investigation ran, so the only response we could observe was the 429 path. The May 2026 incident's documented behavior is `success:true` with no price for items that normally have prices — indistinguishable per-item from `success:true` with no price for items that genuinely have no listings (the rare-baseline case). Until we capture a degraded response with a distinguishing field (or Steam publishes an error code), the cycle-level heuristic is the best signal we have.

For the other sources, per-item detection works fine:

- **Skinport:** HTTP 429 retry-exhaustion (all items declined, RateLimited raised) vs `min_price:null` per item (unavailable). Clean per-item rule; heuristic won't fire in practice.
- **DMarket:** HTTP 429 retry-exhaustion (RateLimited) vs `objects:[]` per item (unavailable). Clean per-item rule; heuristic won't fire in practice.

Universal application of the heuristic is harmless — at the threshold + Skinport/DMarket signal shape, the rule never fires from healthy operation.

### 4. Response evidence (5a)

Steam's 429 hard-ban shape, captured 12 May 2026 around 20:44 UTC from the same host the collector container runs on:

```
HTTP/1.1 429 Too Many Requests
Server: nginx
Content-Type: application/json; charset=utf-8
Vary: Accept-Encoding, Origin
X-Frame-Options: SAMEORIGIN
Content-Encoding: gzip
Vary: Accept-Encoding
Content-Length: 24
Date: Tue, 12 May 2026 20:44:26 GMT
Connection: keep-alive

null
```

Notable:

- **No `Retry-After` header.** Steam does not consistently send one. The fallback ladder is the operative pause-duration path for Steam.
- **Body is literally `null`** (4 bytes JSON-decoded; 24 bytes gzipped). No structured error, no item-name echo.
- **Manifestly-fake item names also 429.** Confirms the 429 fires at the IP layer before any item-existence check. The hard-ban shape is whole-IP, not per-item; no clever query-shape gymnastics will work around it.

Healthy baseline shape (from production logs prior to the event, per `docs/sources-and-semantics.md`):

```json
{
  "success": true,
  "lowest_price": "$12.34",
  "median_price": "$12.50",
  "volume": "99"
}
```

And the `success:false` shape (also from production logs) — the ambiguous case the heuristic addresses:

```json
{
  "success": true
}
```

That is: HTTP 200, success=true, no `lowest_price`, no `median_price`. Steam returns this both for genuinely-listing-less items (the rare baseline) AND, during the May 2026 event, for items that normally have hundreds of listings. The collector cannot disambiguate at the per-item layer. **The cycle-level heuristic is a fallback design awaiting better empirical evidence at the next degradation event** — if the next event captures a degraded response with a distinguishing field, the per-item rule should replace the heuristic.

Skinport and DMarket per-item failure shapes are well-characterized by their respective collector ADRs (008 §2 and 012 §3); not re-captured here.

### 5. Job IDs renamed to `{source_name}_cycle`

APScheduler job IDs use the full source name (`steam_market_cycle`, `skinport_cycle`, `dmarket_cycle`) rather than the prior shorthand (`steam_cycle`, `skinport_cycle`, `dmarket_cycle`). Two reasons:

- The `_apply_pause(source_name, ...)` path needs a deterministic mapping from source name to job ID. `f"{source_name}_cycle"` is the simplest such mapping.
- Operator-visible log lines (`docker compose logs collector | grep cycle`) now use the same identifier as `sources.name` in the DB. `grep "Steam cycle complete"` still works (`source_label` unchanged), but APScheduler's own logs (`Job steam_market_cycle executed successfully`) now line up cleanly with `SELECT * FROM sources`.

Skinport's job ID is unchanged (`skinport_cycle`); only `steam_cycle` → `steam_market_cycle` actually changed. Minor, internal, called out for greppability.

## Operator workflow after this phase

1. **Rebuild the collector image** so the running container has the new code:

   ```bash
   docker compose build collector
   ```

   `docker compose restart` alone is **not sufficient** — it restarts the container against whatever image was previously built. A Task 9 fire-drill on 2026-05-12 hit exactly this: the collector was restarted post-commit but with the pre-commit image, so Steam continued retrying item-by-item past 429-exhaustion (the old code returned None on exhaust; the new code raises RateLimited). Logs showed no `rate-limited`, no `cycle aborted by RateLimited`, no `paused` lines because the new log lines didn't exist in the running binary. The fix was `docker compose build collector` followed by `docker compose up -d collector`. The behavior under the new image, captured live:

   ```
   21:25:05  Steam 429 (attempt 1/5) for 'AK-47 | Asiimov (Battle-Scarred)' — Retry-After=absent, sleeping 0.11s
   21:25:14  Steam 429 (attempt 2/5) … sleeping 8.42s
   …
   21:25:51  Steam 429 (attempt 5/5) … sleeping 47.65s
   21:26:39  ERROR  Steam collector exhausted 5 attempts for 'AK-47 | Asiimov (Battle-Scarred)'
   21:26:39  WARN   Steam cycle aborted by RateLimited after 0 items consumed
   21:26:39  INFO   Steam cycle complete: 48 attempted, 0 written, 0 unchanged, 0 unavailable, 0 declined, 0 lookup_failed
   21:26:39  WARN   Steam rate-limited (Retry-After=absent) — pausing job for 300s
   21:26:39  WARN   steam_market job paused until 2026-05-12T21:31:39+00:00 (in 300s)
   ```

   Skinport and DMarket cycles ran to completion in parallel during Steam's retry-then-pause sequence, confirming the per-source independence.

   The integration test `test_collect_cycle_aborts_on_first_item_ratelimited` in `tests/test_steam_collector.py` is the in-pytest guard against a real propagation regression — if the bug had been in code, that test would have surfaced it. (It passed against the committed code, which is what flagged the fire-drill as image-not-code.)

2. Re-enable Skinport in the DB:

   ```sql
   UPDATE sources SET enabled = TRUE WHERE name = 'skinport';
   ```

   (No need to update `interval_minutes` or `per_item_delay_seconds` — migration 0003 already set Skinport to 15/0.)

3. `docker compose up -d collector` — re-reads `sources` and registers `skinport_cycle` again.

4. Watch one cycle complete cleanly: `docker compose logs --tail 200 collector | grep -E "cycle complete|rate-limited|paused"` — expect a healthy Skinport `N written, M unchanged, 0 declined` line, and (assuming Steam is still IP-banned) a one-time Steam `rate-limited … pausing job for 300s` line followed by no further Steam activity for 5 minutes.

5. Phase 6 (FastAPI read API) is greenlit only after that verification.

## Consequences

- **Pro:** the scheduler now respects `sources.enabled` as the single switch the analytics layer already respects. Adding a fourth source is one row insert + Collector subclass + entry in `SOURCE_REGISTRY` — no scheduler refactor.
- **Pro:** `Retry-After` is honored when present (in-call layer caps at 60s; job-pause layer uses it directly). Sources that send the header get respectful backoff.
- **Pro:** the `unavailable` vs `declined` split preserves the "item has no listings" signal that the bot will eventually surface. Declined counts give the operator a metric for "is the rate-limit fix working" — should be near-zero in healthy operation.
- **Con:** the cycle-level heuristic for Steam's ambiguous response is a fallback awaiting better evidence. The next degradation event (when Steam doesn't 429 our IP) is a chance to capture the per-item degraded shape and replace the heuristic with a per-item rule.
- **Con:** the rate-limit memory dict (`_rate_limit_state`) is in-process. A scheduler restart loses the doubling-ladder state; the next 429 resets to the 5min initial. Acceptable at v1 — the scheduler restarts rarely, and the worst case is one extra 5min pause where the ladder might have set 20min. Persistent state would be a `scheduler_state` table; not implemented.
- **Con:** if the operator forgets the Task 9 step (`UPDATE sources SET enabled = TRUE WHERE name = 'skinport'`), Skinport silently stays off. The cycle-complete-grep workflow surfaces this on first verification. Not a soft failure — visible in `SELECT name, enabled FROM sources`.
