# Pre-Phase 2 diagnostics — 2026-05-15

Diagnostic-only. No code changes, no fixes proposed below the surface of "here's what the next phase should address". Two questions, traced through code and the live DB.

**Headline.** Phase 2 is **still greenlit**. The Steam $1.00 SC on Redline is isolated contamination from one test module writing to the production DB; not a production-data issue. Two genuine follow-ups land in Phase 2's lap (or earlier): (a) the test-pollution source needs to stop writing to the real watchlist item, and (b) `/deals/evaluate` carries the same `prices.timestamp` freshness bug Phase 1 already fixed in `/items/{slug}/price`.

---

## Question 1 — what does `/deals/evaluate` actually require?

### 1.1 Conditions for `no_comparable_data` vs an actual verdict

The verdict-vs-`no_comparable_data` decision is in `api/routes/deals.py:208-229`:

```python
def _decide_verdict(...) -> tuple[str, str]:
    ...
    if not comparable:
        return (
            "no_comparable_data",
            (
                f"No fresh comparable data for {display_name} in "
                f"{currency_label}. "
                f"{len(informational)} informational source(s) returned "
                f"— see the `informational` block for context."
            ),
        )
    cheapest = min(c.current for c in comparable)
    ...
```

A row becomes **comparable** only if BOTH of these are true in the loop at lines 98-151:

- `row["denomination"] == offer.currency` (passes the stage-1 check at line 102).
- `row["observed_at"] >= freshness_floor` where `freshness_floor = now - timedelta(hours=4)` (passes the stage-2 check at line 119).

Anything that flunks either stage is appended to `informational` with `reason="denomination_mismatch"` (line 110) or `reason="stale"` (line 126), respectively, and excluded from the verdict math.

If `comparable` ends up empty, the function returns `no_comparable_data`. Otherwise the verdict is decided by comparing `offer.amount` to `min(comparable.current)` with a ±5% tolerance band (`AT_MARKET_TOLERANCE_PCT = Decimal("0.05")`, line 48):

```python
lower = cheapest * (Decimal("1") - AT_MARKET_TOLERANCE_PCT)
upper = cheapest * (Decimal("1") + AT_MARKET_TOLERANCE_PCT)
if offer_amount < lower:   verdict = "below_market"
elif offer_amount > upper: verdict = "above_market"
else:                      verdict = "at_market"
```

### 1.2 Are USD and wallet_credit treated as comparable to each other?

**No, filtered apart unconditionally.** The stage-1 currency split at `deals.py:99-114` is hard:

```python
if row["denomination"] != offer_currency:
    note = _denomination_note(row["denomination"], offer_currency)
    informational.append(
        InformationalSource(..., reason="denomination_mismatch", note=note)
    )
    continue
```

The `_denomination_note` helper (lines 172-198) even spells out the rationale in human-readable form (wallet credit can't be withdrawn, etc.). No conversion factor is ever applied; the code comment at line 101 calls this out explicitly: *"no amount of freshness rescues a wallet-credit price into a USD comparison."*

### 1.3 Minimum number of comparable sources for a verdict

**One.** The check is `if not comparable:` (line 220) — a single fresh, currency-matched source is enough to anchor the verdict. `min(c.current for c in comparable)` collapses to that one row.

### 1.4 Which timestamp does the freshness filter use?

**It uses `prices.timestamp` — the pre-Phase-1 pattern, and the same dedup-vs-display bug Phase 1 fixed in `/items/{slug}/price`.**

The query at `deals.py:76-92`:

```sql
SELECT DISTINCT ON (p.source_id)
    s.name AS source_name,
    s.denomination,
    p.price,
    p.timestamp AS observed_at
FROM prices p
JOIN sources s ON s.id = p.source_id
WHERE p.item_id = :item_id
  AND s.enabled = TRUE
ORDER BY p.source_id, p.timestamp DESC
```

…and the freshness check at line 119:

```python
if row["observed_at"] < freshness_floor:
    informational.append(... reason="stale" ...)
    continue
```

`p.timestamp` is the dedup-on-write column from ADR 009 §3; for a polled-cleanly-but-flat item it lags behind `observation_log.last_observed_at` by hours. This means the deals endpoint demotes Skinport (and friends) to `reason="stale"` even when the collector is hitting the source every 15 minutes with zero failures — exactly the symptom Phase 1 fixed elsewhere.

**Status.** This is the follow-up flagged in `PROJECT_OVERVIEW.md §5`'s resolved-note and in ADR 017 §4. Not patched in Phase 1 to keep that diff reviewable. Worth fixing before or as part of Phase 2.

### 1.5 Why AK-47 Redline FT got `above_market` and Desert Eagle Blaze FN got `no_comparable_data`

I ran the exact `/deals/evaluate` query against the live DB for both items:

```
              slug              |    source    | denomination  |  price  |          observed_at          | would_be_fresh
--------------------------------+--------------+---------------+---------+-------------------------------+----------------
 ak-47-redline-field-tested     | dmarket      | usd           |   30.96 | 2026-05-15 19:38:43+00        | t
 ak-47-redline-field-tested     | skinport     | usd           |   29.49 | 2026-05-15 21:38:38+00        | t
 ak-47-redline-field-tested     | steam_market | wallet_credit |    1.00 | 2026-05-15 21:26:20+00        | t
 desert-eagle-blaze-factory-new | skinport     | usd           |  754.90 | 2026-05-15 14:38:38+00        | f
 desert-eagle-blaze-factory-new | steam_market | wallet_credit | 1050.00 | 2026-05-15 10:21:22+00        | f
```

The DMarket row for Desert Eagle Blaze is absent — confirmed by:

```
slug                              | source       | obs_log              | latest_prices_ts
desert-eagle-blaze-factory-new    | dmarket      | (null)               | (null)
desert-eagle-blaze-factory-new    | skinport     | 2026-05-15 21:53:38  | 2026-05-15 14:38:38
desert-eagle-blaze-factory-new    | steam_market | 2026-05-15 21:21:22  | 2026-05-15 10:21:22
```

**AK Redline FT path (assume a USD offer above $30.96):**

- Skinport row: `denomination='usd'` → passes stage 1. `observed_at = 21:38:38` is < 4h old → passes stage 2. Appended to `comparable` with `current=$29.49`.
- DMarket row: same — `usd`, fresh — `comparable` with `current=$30.96`.
- Steam row: `denomination='wallet_credit' != 'usd'` → stage 1 demotes to `informational, reason="denomination_mismatch"` (the bogus $1.00 doesn't even reach the math).
- `comparable` = 2 entries. Cheapest = $29.49 (Skinport). Tolerance band = $28.02–$30.96. An offer above $30.96 → `above_market`.

**Desert Eagle Blaze FN path (assume a USD offer):**

- Skinport row: `denomination='usd'` → stage 1 passes. `observed_at = 14:38:38`, now ≈ 21:54 → ~7h15m old, > 4h freshness floor → **stage 2 demotes to `informational, reason="stale"`.** Excluded from the verdict.
- Steam row: `denomination='wallet_credit' != 'usd'` → stage 1 demotes to `informational, reason="denomination_mismatch"`.
- DMarket: no row in the `DISTINCT ON` result (title-mismatch casualty — there's no `observation_log` row and no `prices` row for `(deagle-blaze, dmarket)`).
- `comparable` = 0 entries → `no_comparable_data`.

The Skinport demotion is the load-bearing one. The Skinport collector observed Deagle Blaze 14 seconds ago (`observation_log.last_observed_at = 21:53:38`); the price just hasn't moved in 7h15m, so `prices.timestamp` lags. The endpoint is reading the wrong timestamp. **If the deals endpoint were already on `observation_log.last_observed_at` (the Phase 1 fix applied here), Skinport at $754.90 would have anchored a real verdict for this item.** That's the bug to fix; the rest of the path is functioning as designed.

---

## Question 2 — why is Steam reporting 1.00 SC for AK-47 Redline FT?

**TL;DR: it's not Steam, it's a test module writing to the production DB.** The corrupt row's `raw_response` is `{"test": true}` — a payload the Steam collector would never produce. The path that wrote it bypasses the outlier filter by construction.

### 2.1 Last 20 Steam observations for Redline

```
         ts          | price | volume | raw_lowest | raw_median | raw_volume | raw_success
---------------------+-------+--------+------------+------------+------------+-------------
 2026-05-15 21:26:20 |  1.00 |      1 |            |            |            |             ← bogus
 2026-05-15 21:20:20 | 42.45 |    103 | $42.45     | $42.00     | 103        | true
 2026-05-15 20:20:20 | 43.72 |    102 | $43.72     | $42.94     | 102        | true
 2026-05-15 19:20:20 | 43.53 |    113 | $43.53     | $44.42     | 113        | true
 …                   (17 more rows, all genuine $40-$44 readings)
```

One row at `21:26:20` is bogus; the preceding 19 are clean Steam payloads with `success: true`, parseable `lowest_price`/`median_price`, and realistic volumes (100–130). Inspecting the raw_response JSONB of the latest 3:

```
2026-05-15 21:26:20 | 1.00  | 1   | {"test": true}
2026-05-15 21:20:20 | 42.45 | 103 | {"volume": "103", "success": true, "lowest_price": "$42.45", "median_price": "$42.00"}
2026-05-15 20:20:20 | 43.72 | 102 | {"volume": "102", "success": true, "lowest_price": "$43.72", "median_price": "$42.94"}
```

`{"test": true}` is not a shape any code in `collectors/steam.py` produces. It's a fixture payload from the test suite.

### 2.2 Last 50 Steam observations across all items

50 rows returned, ordered DESC. Only ONE row across all items has a non-Steam-shaped `raw_response` — the same Redline row at `21:26:20`. Every other row carries either `{"success": true, "lowest_price": …}` or `{"success": true, "median_price": …}`, all with realistic prices ranging from $6.16 (AK-47 Slate FT) to $1,909.25 (Karambit Doppler FN).

A more aggressive sweep across all of `prices` for any test-marker payload:

```sql
SELECT i.market_hash_name, s.name AS source, count(*) AS test_rows,
       min(p.timestamp), max(p.timestamp)
FROM prices p
JOIN items i ON i.id = p.item_id
JOIN sources s ON s.id = p.source_id
WHERE p.raw_response @> '{"test": true}'::jsonb
   OR p.raw_response @> '{"baseline": true}'::jsonb
   OR p.raw_response @> '{"synthetic": true}'::jsonb
GROUP BY i.market_hash_name, s.name;
```

Result: **2 rows total**, both on AK-47 Redline FT / Steam. One from `2026-05-14 00:48:23`, one from `2026-05-15 21:26:20`. Both have `raw_response = {"test": true}`. No other item or source carries any test-marker payload.

### 2.3 The Steam outlier filter — what it does, and the Redline math

The filter is at `collectors/steam.py:144-158`:

```python
def _is_steam_outlier(median, observed_price) -> bool:
    if median is None:
        return False
    threshold = median * STEAM_OUTLIER_THRESHOLD_PCT  # 0.20
    return observed_price < threshold
```

It's called from `SteamCollector.collect_one` at line 316-329 — *after* `_parse_response` produces a `PriceObservation` and *before* the cycle persists it:

```python
median = _seven_day_median(obs.market_hash_name)
if _is_steam_outlier(median, obs.price):
    threshold = median * STEAM_OUTLIER_THRESHOLD_PCT
    logger.warning("Steam outlier filter: rejecting %r at price=%s …", …)
    return DECLINED
return obs
```

The 7-day median for Redline (replicating `_seven_day_median`'s query at lines 117-134):

```
rows_in_window | median_price | min_price | max_price
----------------+--------------+-----------+-----------
             85 |        43.37 |      1.00 |     63.83
```

85 observations in the 7-day window (well above `STEAM_OUTLIER_MIN_OBSERVATIONS = 5`), median = $43.37. The min of $1.00 in the window is the corrupt row itself; one outlier doesn't shift the median materially (the median is still in the genuine $40-$44 cluster).

Threshold = `$43.37 × 0.20 = $8.67`. The bogus observation of $1.00 is well below that. **The filter, if called, would have logged a warning and returned `DECLINED`.** It just wasn't called.

### 2.4 Why the filter is failing to catch this

Not because the median is corrupted (it isn't — see 2.3) and not because the threshold is wrong. The filter is being **bypassed entirely**. Trace:

The corrupt row was written by `tests/test_scheduler.py`. The smoking gun is at `tests/test_scheduler.py:55`:

```python
_TEST_ITEM_NAME = "AK-47 | Redline (Field-Tested)"  # ← real watchlist item
```

…and the helper at `tests/test_scheduler.py:78-89`:

```python
def _make_obs(price, volume, *, source="steam_market"):
    return PriceObservation(
        market_hash_name=_TEST_ITEM_NAME,
        source_name=source,
        timestamp=datetime.now(UTC),
        price=Decimal(price) if price is not None else None,
        volume=volume,
        currency="USD",
        raw_response={"test": True},      # ← exact payload found in DB
    )
```

This is called with `_make_obs("1.00", 1)` at `tests/test_scheduler.py:550` and `:574` inside the `TestRunCycleDeclinedHeuristic` class. Those tests:

1. Build a `_FakeCollector` (defined at `tests/test_scheduler.py:491-528`) that scripts a list of pre-built `PriceObservation`s.
2. Pass it to the production `_run_cycle(collector, "Fake")` in `collectors/scheduler.py`.
3. `_run_cycle` iterates the watchlist, asks the collector for observations, and persists each one through the standard persist path (`should_write_observation` + `persist_observation`).

The dedup gate doesn't block this — `(price=1.00, volume=1)` differs from the prior latest `(42.45, 103)`, so `should_write_observation` returns `True` and the row gets written.

**The outlier filter doesn't run because it lives inside `SteamCollector.collect_one`** (line 316), and `_FakeCollector.collect_one` raises `NotImplementedError` (line 510-511). The fake's `collect_cycle` yields pre-built observations directly, bypassing both the HTTP call and the outlier check.

There's no per-test cleanup either: `TestRunCycleDeclinedHeuristic` doesn't use the `session_with_baseline_row` fixture (which does clean up at far-future timestamps); it writes rows at `datetime.now(UTC)` and leaves them.

So the pollution mechanism is end-to-end:

1. Pytest run hits `TestRunCycleDeclinedHeuristic` → builds `_FakeCollector` with `_make_obs("1.00", 1)` → `_run_cycle` writes to the real `prices` table against the real Redline `items.id` → outlier filter not invoked → no cleanup → corrupt row lingers as the latest Steam observation for Redline until the next genuine Steam cycle writes a row.

The most recent Steam cycle for Redline ran at `21:20:20`, completed cleanly with `$42.45`. The test then ran at `21:26:20`, overwriting "latest" with `$1.00`. Next Steam cycle is at `22:20:20`, which will return a real reading and supplant the bogus latest — but until then the bot's `query_current_price` will keep reporting `1.00 SC`.

### 2.5 What did Steam actually return for the latest legitimate poll?

The row immediately preceding the corrupt one is the answer to "is the issue in the response or in our parsing":

```
2026-05-15 21:20:20 | 42.45 | 103 | {"volume": "103", "success": true,
                                    "lowest_price": "$42.45", "median_price": "$42.00"}
```

Steam returned `lowest_price: "$42.45"`. The parser at `collectors/steam.py:168-186` strips everything except digits and the decimal point and produces `Decimal("42.45")`. The parsed price (42.45) matches the `lowest_price` raw value exactly. **No issue in the response or the parsing path.** This row is exactly what we expect, and it would still be "latest" if the test hadn't overwritten it.

### 2.6 Cross-item sweep — items where Steam ≪ Skinport even after SC→USD discount

Using the brief's heuristic — Steam × 0.85 < 5% of Skinport — applied to the latest reading per (item, source):

```sql
WITH latest_per_source AS (
  SELECT DISTINCT ON (p.item_id, p.source_id)
    p.item_id, p.source_id, p.price, p.timestamp, p.raw_response
  FROM prices p
  JOIN sources s ON s.id = p.source_id
  WHERE s.name IN ('steam_market', 'skinport')
  ORDER BY p.item_id, p.source_id, p.timestamp DESC
)
SELECT i.market_hash_name,
       sp.price AS skinport_usd, st.price AS steam_sc,
       round((st.price * 0.85)::numeric, 2) AS steam_usd_equiv,
       round((st.price * 0.85 / NULLIF(sp.price, 0) * 100)::numeric, 1) AS pct_of_skinport,
       st.raw_response::text AS steam_raw
FROM items i
JOIN latest_per_source st ON st.item_id = i.id
  AND st.source_id = (SELECT id FROM sources WHERE name='steam_market')
JOIN latest_per_source sp ON sp.item_id = i.id
  AND sp.source_id = (SELECT id FROM sources WHERE name='skinport')
WHERE st.price * 0.85 < 0.05 * sp.price;
```

Result:

```
       market_hash_name        | skinport_usd | steam_sc | steam_usd_equiv | pct_of_skinport | steam_raw
-------------------------------+--------------+----------+-----------------+-----------------+------------------
 AK-47 | Redline (Field-Tested)|        31.03 |     1.00 |            0.85 |             2.7 | {"test": true}
```

**Exactly one item matches: Redline.** No other item across the watchlist shows a suspiciously-low Steam reading.

---

## Findings summary

| | Question | Answer |
|---|---|---|
| **Q1** | `no_comparable_data` trigger | `comparable` list empty after stage-1 denomination split and stage-2 freshness split. |
| **Q1** | USD vs wallet_credit comparability | Hard split, never comparable; demoted to informational unconditionally. |
| **Q1** | Min comparables for verdict | 1. |
| **Q1** | Freshness uses `prices.timestamp`? | **Yes** — same Phase 1 bug, different endpoint. Follow-up flagged in ADR 017 §4 and `PROJECT_OVERVIEW.md §5`. |
| **Q1** | Deagle Blaze no_comparable_data root cause | Skinport demoted to `reason="stale"` because `p.timestamp` is 7h old, even though `observation_log` shows Skinport polled cleanly 14 seconds ago. Steam denom-mismatch + stale. DMarket no row (title-mismatch). Fix the deals freshness filter → verdict materializes. |
| **Q2** | Latest Redline Steam row source | `tests/test_scheduler.py::TestRunCycleDeclinedHeuristic` — `_make_obs("1.00", 1)` hardcoded against the real watchlist item, persisted via `_FakeCollector` + real `_run_cycle`. |
| **Q2** | Outlier filter — would have caught? | Yes. Median $43.37, threshold $8.67, observed $1.00. But the filter sits in `SteamCollector.collect_one`; `_FakeCollector` bypasses it. |
| **Q2** | Median corruption from past pollution? | No. Only 2 polluted rows in total (May 14 and May 15, both on Redline); median is still in the legitimate $40-44 cluster. |
| **Q2** | Cross-item Steam-vs-Skinport sweep | Only Redline. Every other (item, Steam) pair has a payload consistent with a real Steam response. |

## Phase 2 status

**Greenlit, pending small fixes:**

- **Steam pollution** is isolated (2 rows, 1 item). Production data is healthy. The next Steam cycle for Redline at ~22:20 UTC will write a real observation, and the bot will resume reporting genuine $40-44 readings — no manual cleanup strictly required, though the polluted row will linger in history. Worth a short fix to `tests/test_scheduler.py` (use a sentinel item like the `test_api.py` pattern, or fake-collector-write to an `__TestScheduler__` fixture item) before Phase 2 starts, so re-running the suite doesn't keep re-poisoning the live DB. Not a blocker.

- **`/deals/evaluate` freshness filter** carries the same Phase 1 dedup-vs-display bug `/items/{slug}/price` had. It manifests visibly today: Desert Eagle Blaze FN returns `no_comparable_data` despite Skinport being polled cleanly 14 seconds ago. This is the follow-up already flagged in ADR 017 §4 and `PROJECT_OVERVIEW.md §5`. Fixing it is small (mirror the items.py LATERAL pattern), high-value, and a natural opener for Phase 2.

Neither finding is a hard blocker. Phase 2 may proceed.
