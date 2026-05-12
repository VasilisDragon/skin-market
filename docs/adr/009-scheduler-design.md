# ADR 009 — Scheduler design

**Status:** Accepted
**Date:** 2026-05-11
**Related:** ADR 006 (collector resilience), ADR 008 (Skinport collector)

## Context

Phase 4 introduces a long-running scheduler that drives both collectors
on cron-like intervals (Steam every 30 min, Skinport every 5 min). The
service runs in Docker, restarts on the Spark across reboots, and must
not silently lose or duplicate data. The design has several non-obvious
choices that we want pinned now, before Phase 5 layers analytics on
top of accumulated price rows.

## Decisions

### 1. `BlockingScheduler`

APScheduler offers three scheduler classes. We use `BlockingScheduler`:

- `BlockingScheduler` blocks the calling thread on `start()` and returns
  when `shutdown()` is called. Perfect for "this is the whole process."
- `BackgroundScheduler` runs a daemon thread; we'd need a `while True:
  sleep` keep-alive in main to stop the container from exiting.
  Strictly more code for no benefit.
- `AsyncIOScheduler` integrates with an asyncio event loop. Our
  collectors are sync (`httpx.Client`, not `AsyncClient`); introducing
  async here just to run the scheduler is gratuitous.

The container's `CMD` is `python -m collectors.scheduler`. The process
exits cleanly when `BlockingScheduler.start()` returns from a
`shutdown()` call.

### 2. Overlap policy

Both jobs are configured with APScheduler `job_defaults`:

```python
{
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 300,
}
```

- **`max_instances=1`**: if a cycle is still running when its next
  interval ticks, the next firing is dropped (with a "missed execution"
  log line from APScheduler). Prevents concurrent hits on the same
  upstream API.
- **`coalesce=True`**: if several scheduled firings have been missed
  (e.g. process slept), they collapse into a single execution rather
  than firing a backlog at once.
- **`misfire_grace_time=300` seconds**: tolerate up to 5 minutes of
  clock skew / GC pause without dropping a legitimate firing.

At v1 cadences, overlap should never happen — Steam's ~4-minute cycle
fits comfortably in 30-minute ticks, Skinport's ~1-second cycle fits
trivially in 5-minute ticks. The policy is for the failure case
(an upstream that's slow to respond), not steady state.

### 3. Conditional writes: exact equality

Every observation is compared against the most recent row for the same
`(item_id, source_id)` via `collectors.base.should_write_observation`
**before** persistence. Exact equality on `(price, volume)`:

```python
return (latest.price, latest.volume) != (obs.price, obs.volume)
```

No tolerance threshold. Reasons:

- Tolerances are an arbitrary bug source. "Within 1%" or "within $0.01"
  is a decision someone makes once and forgets to revisit, then
  analytics has to remember it forever.
- Cent-level changes ARE meaningful market signal — a price moving
  $42.92 → $42.93 is a real datapoint, not noise.
- ON CONFLICT DO NOTHING at the SQL layer already handles
  same-timestamp races; the conditional check handles the much more
  common case of "this is a fresh observation but its content is
  identical to the last one."

Where the logic lives: in the **scheduler**, not in
`persist_observation`. The scheduler is the policy layer; persistence
is a primitive that does what it's told. A future ad-hoc tool that
wants to force-write a duplicate doesn't have to fight the persistence
function. The CLI entrypoints (`python -m collectors.steam --item`)
intentionally bypass the conditional check because their job is to
demonstrate a single fetch, not to participate in the dedup regime.

### 4. Catastrophic-failure handling

Each scheduled job is wrapped in a try/except in its top-level
function (`run_steam_cycle`, `run_skinport_cycle`):

```python
def run_steam_cycle():
    try:
        _run_cycle(SteamCollector(), "Steam", watchlist_limit=50)
    except Exception:
        logger.exception("Steam cycle failed with unhandled exception")
```

Why: APScheduler treats an uncaught exception in a job as a job
failure that does not stop the scheduler, but it routes the traceback
to APScheduler's own logger and skips the cycle summary line. We want
the cycle summary in our standard log format with the full traceback
attached. Wrapping ensures it.

For real process death (OOM kill, segfault in libpq, etc.):
Docker's `restart: unless-stopped` brings the container back. The
scheduler is **stateless** — every piece of state lives in Postgres —
so a restart is always safe. No checkpointing, no resume logic, no
in-flight state to recover.

No health endpoint in v1. The operator runs
`docker compose logs --tail 50 collector` for status; `grep "cycle complete"`
gets the operational summary.

### 5. Logging

Structured-ish JSON to stdout, same format as the collector modules:

```
{"ts":"2026-05-11 19:00:00,000","level":"INFO","name":"collectors.scheduler","msg":"Steam cycle complete: 48 attempted, 47 written, 0 unchanged, 1 unavailable, 0 lookup_failed"}
```

Greppable: `docker compose logs collector | grep "cycle complete"` is
the one-liner for operational status. The four counters split the
cycle's outcome into mutually exclusive buckets:

- `written`: row landed in `prices`
- `unchanged`: dedup skipped — same `(price, volume)` as the most
  recent row
- `unavailable`: collector returned None — Steam `success:false`,
  Skinport `min_price:null`, or retry exhaustion
- `lookup_failed`: item or source name missing from DB (defensive;
  should be 0 in steady state)

`attempted` is the count of items the cycle tried to fetch (i.e.
`written + unchanged + unavailable + lookup_failed`).

### 6. SIGTERM / graceful shutdown

```python
def shutdown(signum, _frame):
    logger.info(
        "Signal %d received; finishing in-flight cycles and shutting down",
        signum,
    )
    scheduler.shutdown(wait=True)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
```

`scheduler.shutdown(wait=True)` blocks until every running job
finishes. `BlockingScheduler.start()` then returns and the process
exits 0.

The companion compose setting `stop_grace_period: 5m` gives Steam's
~4-minute cycle room to drain. If the operator really wants a faster
stop (`docker compose down --timeout 5`), Docker SIGKILLs after 5
seconds. Each `persist_observation` is an independent statement, so
the worst a SIGKILL costs is the in-flight item, not the cycle's
prior writes.

### 7. Watchlist rotation (deferred)

Spec says "Steam every 30min (50 items/cycle, rotating through
watchlist)". Our watchlist is 48 items; we pass `watchlist_limit=50`
which is a no-op slice for the current list. If the watchlist grows
past 50, this becomes a real rotation problem and `_load_watchlist`
needs to track a per-cycle offset (probably persisted in a
`scheduler_state` table or computed from a wall-clock-derived index).
Not implemented; flagged in the source.

## Consequences

- **Pro:** the scheduler is small (~150 lines), single-process, and
  has no state beyond what's in Postgres. Restart is always safe.
- **Pro:** the conditional-write rule reduces Skinport's projected
  ~13.5k rows/day to whatever fraction of the watchlist actually
  changes price+volume per 5-minute cycle. Quiet items will be
  near-zero; volatile knives will be most of the writes.
- **Con:** `attempted = written + unchanged + unavailable +
  lookup_failed` is four counters. Phase 5 analytics that wants
  "items with at least one new price today" should query the
  `prices` table directly, not derive it from cycle logs.
- **Con:** stop_grace_period is 5 minutes. On a `docker compose down`
  during a Steam cycle, the shell waits. Operators who don't know
  this will think Docker hung. Documented in the README's deploy
  notes (Phase 8) when it lands.
- **Related:** the conditional-write check is one SELECT per
  observation per cycle. At 48 items × 2 sources × 12 cycles/hour =
  ~1100 extra lookups/hour. The composite-PK index on `prices`
  answers each in microseconds; Postgres won't notice.
