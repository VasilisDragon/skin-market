# ADR 006 — Collector resilience strategy

**Status:** Accepted
**Date:** 2026-05-11

## Context

The Steam Market collector polls a public API that is, by design,
unfriendly to non-browser clients. It rate-limits aggressively, blocks
Python's default User-Agent, responds slowly under load, and
occasionally returns `success: false` for items that have no current
listings. The Skinport collector (Phase 3) is more permissive but
still subject to the same general failure modes.

Before writing the first collector we need explicit policies for:

1. Which HTTP outcomes trigger retries vs. immediate skip.
2. How the retry delay is computed.
3. What `success: false` and parse failures mean for the time series.
4. What we do when Steam starts blocking us *despite* our retries.

## Decision

### 1. HTTP outcome taxonomy

| Outcome | Action | Log level |
|---|---|---|
| 200 with parseable JSON, `success: true`, price present | Persist | INFO (only if needed) |
| 200 with `success: false` | Skip, no DB write | INFO |
| 200 with `success: true` but unparseable / missing price | Skip, no DB write | WARNING |
| 200 with non-JSON body | Skip, no DB write | WARNING |
| 429 | Retry with backoff | WARNING |
| 5xx | Retry with backoff | WARNING |
| 4xx (not 429) | Skip, no DB write, no retry | WARNING |
| Network timeout / connection error | Retry with backoff | WARNING |
| Retry exhaustion (5 attempts) | Skip, no DB write | ERROR |

### 2. Backoff: AWS full-jitter

`sleep_seconds = uniform_random(0, min(cap, base · 2^attempt))`

With `base=5s`, `cap=300s`, `max_attempts=5`. This is the AWS-published
full-jitter algorithm: faster recovery than exponential without jitter,
and resilient to thundering herd because each retry is independently
randomized.

The 5-attempt cap is a compromise. Too few attempts means a single
transient hiccup discards a whole cycle's data. Too many means a real
upstream outage gets long after we should have given up and logged
loudly. Five attempts at base=5s give 0-300s of total potential delay
per item — bounded enough that a 50-item cycle can't be derailed by
one stuck item.

### 3. `success: false` is data, not error — but we don't store it as price

`success: false` means "the item exists on Steam but has no current
listings." That's meaningful information — it correlates with imminent
price moves. But it is **not a price**, and writing a NULL price row
would corrupt every moving-average and aggregate downstream.

Two paths considered:

- **Skip-and-log (chosen for v1).** No row written. Logged at INFO.
  If the analytics layer later needs the "no listings" signal, we add
  an `availability` table that records (item, source, timestamp,
  was_listed) without a price column. v1 doesn't need that yet.
- **Write a NULL-price row.** Rejected because every downstream
  consumer would have to remember to filter `WHERE price IS NOT NULL`,
  and the first one to forget breaks analytics silently.

### 4. Cookie / session strategy (planned, NOT implemented in Phase 2)

Steam will eventually 429-block any IP that polls the priceoverview
endpoint enough times, even with conservative rate limiting and a real
browser UA. When that happens, the recovery plan:

1. **`STEAM_SESSION_COOKIE` env var.** Operator logs into Steam in a
   browser, exports the `steamLoginSecure` cookie, drops it in `.env`.
   `SteamCollector.make_client` reads it and adds it as a `Cookie:`
   header. Estimated dev time: 30 minutes.
2. **Cookie rotation** if we ever have multiple Steam accounts. Round
   robin per cycle. Estimated dev time: 2 hours.
3. **Residential proxy pool.** Expensive, complex, only worth it if
   v1.x demand outgrows what authenticated polling can sustain. Defer.
4. **TLS fingerprinting** (replace `httpx` with `curl_cffi` to mimic
   a real-browser ClientHello). Adds a significant dependency for an
   uncertain benefit. Defer.

**Trigger** for kicking off step 1: five consecutive cycles with >=50%
of items returning 429 after full backoff. Until then, we run
cookieless and tolerate the occasional gap. The scheduler (Phase 4)
will surface that metric in its log output.

### 5. The User-Agent

`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36`

A real Chrome 130 string. Steam blocks Python's default
`python-httpx/X.Y.Z` UA almost immediately, but accepts a recent
browser UA for a long time. Pinned as a constant
(`collectors.base.DEFAULT_USER_AGENT`); update when Steam starts
demanding newer.

## Consequences

- **Pro:** the failure modes are now explicit; new collectors (Skinport,
  CSFloat) inherit the same vocabulary instead of reinventing it.
- **Pro:** the cookie escalation path is documented before we need it,
  so the response when Steam blocks us doesn't involve a panicked
  decision.
- **Pro:** the time-series stays clean — only real price observations
  land in `prices`. Availability is captured in logs and (later)
  potentially a separate table.
- **Con:** 5 retries x 50 items at base=5s means a single bad cycle
  can take 25 minutes to fully give up. APScheduler should overlap
  cycles only if the previous one completed; otherwise we'd compound
  the backlog. The scheduler (Phase 4) will enforce that.
- **Related:** the persistence layer (`persist_observation`) is
  defensive about unknown items and sources — it logs and returns
  False rather than crashing. Same philosophy: keep one item's
  problem from killing the whole cycle.
