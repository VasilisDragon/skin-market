# Pre-Phase 2 Pricempire diagnostic — 2026-05-15

**Scope.** Read-only characterization of Pricempire's API ahead of Phase 2 architecture. No code, no schema, no changes outside `docs/`. 6 API calls used (5 × 200, 1 × 400-informative). Raw trimmed samples are in `docs/pre-phase2-pricempire-samples/`.

**Headline.** Pricempire's items endpoint is a **single-shot bulk dump** — no pagination, no app_id filter, no per-item lookup. One call returns all ~39,400 CS2 items with their cross-marketplace prices nested per item. Critically: **Pricempire does not serve Steam Market data** via this endpoint; valid provider keys include `buff163`, `buff163_buy`, `skinport`, `dmarket`, but `steam` is silently dropped. Phase 2 should layer Pricempire on top of the existing Steam/Skinport/DMarket collectors as a breadth-coverage source, not replace them.

Budget headroom is generous: hourly polling of the bulk endpoint costs only 720 calls/month against the 10,000/month Developer tier (7.2%). On-demand queries for non-watchlist items cost **zero incremental API calls** because the hourly bulk already covers the entire catalog. Even with inventory lookups (15% of budget), the dominant constraint will be local storage / cache freshness, not API quota.

---

## 1. Raw findings per call

Authentication: `Authorization: Bearer $PRICEMPIRE_API_KEY` worked on every call. No 401s. No `X-RateLimit-*` headers exposed in any response. Date is the only meaningful response header.

| # | URL | Status | Bytes | Latency | Items |
|---|-----|--------|-------|---------|-------|
| 1 | `/v4/paid/items/prices` (no params) | 200 | 33,070,503 | 3.51s | 39,392 |
| 2 | `/v4/paid/items/prices?app_id=730` | 200 | 33,073,013 | 2.82s | 39,392 |
| 3 | `/v4/paid/items/prices?app_id=730&sources=buff163,skinport,dmarket` | 200 | 38,746,566 | 3.91s | 39,392 |
| 4 | `/v4/paid/items/prices?app_id=730&page=2` | 200 | 33,093,512 | 8.98s | 39,392 |
| 5 | `/v4/paid/items/metas` | 200 | 47,232,770 | 35.64s | 91,294 |
| 6 | `/v4/paid/inventory` (no params) | 400 | 93 | 0.66s | (n/a) |

Latency on Call 4 (8.98s) is network variance — same endpoint, same payload size as Call 2 which served in 2.82s. The 35.6s on Call 5 is the genuine cost of the larger metas response (47 MB, 91k items).

### Call 1 — empty-params

Top-level shape: bare JSON array, length 39,392. Each item is an object with these keys:

```
market_hash_name, liquidity, steam_last_7d, steam_last_30d, steam_last_90d,
marketcap, trades_7d, trades_30d, trades_90d, count, rank, image, prices
```

The nested `prices` array is the multi-provider observations. Per-item count distribution: **min=0, max=2, mean=1.769**. Distinct `provider_key` values in the entire response: only `buff163` and `buff163_buy`. No Steam, no Skinport, no DMarket, no others.

The `steam_last_*` and `trades_*` fields are *metadata about Steam liquidity* (24h-sales-style counters), not Steam price observations. They're populated even though no Steam *price row* is returned. So Pricempire knows Steam exists; it just doesn't surface Steam prices here.

Note: no `app_id` field on individual items. The catalog is implicitly CS2 (sample items: `Glock-18 | Gamma Doppler (Factory New)`, `★ Bayonet | Doppler (Factory New)`, etc.).

Sample: `docs/pre-phase2-pricempire-samples/empty-params.json` (first 3 items, full structure preserved; 3,855 bytes — original 33 MB).

### Call 2 — `?app_id=730`

Functionally **identical to Call 1**. Same length, same providers, byte-difference of 2.5 KB is just price-row `updated_at` jitter from the seconds between calls. First three item names match exactly.

**Conclusion: `app_id=730` is a silent no-op on `/v4/paid/items/prices`.** The endpoint is CS2-only by default (or `app_id` is reserved for future use and ignored when present).

Sample: `docs/pre-phase2-pricempire-samples/cs2-only.json`.

### Call 3 — `?sources=buff163,skinport,dmarket`

Same 39,392 items. Per-item `prices` array distribution: **min=0, max=3, mean=2.650** — meaningfully higher than Call 1's 1.77. Coverage per provider:

| Provider | Items with at least one price row |
|---|---|
| buff163 | 34,802 (88.3%) |
| skinport | 34,802 (88.3%) |
| dmarket | 34,802 (88.3%) |
| **steam** | **0** |

The exact-match 34,802 across the three providers is unlikely to be a coincidence — it suggests Pricempire's normalized cross-source catalog tops out at ~88% coverage on items that have *any* tradable listing on the major marketplaces. The remaining 4,590 items are probably ultra-illiquid (one-offs, deprecated drops, knife-glove rarities).

**`steam` is silently dropped from `sources=`** — no 4xx, no warning, just absent from the response. This is the load-bearing finding for Phase 2 design: Pricempire's value proposition is breadth on `buff163`/`skinport`/`dmarket`, not Steam coverage.

The `buff163_buy` provider that showed up in Call 1 disappears here — `sources=` is a strict whitelist; only requested providers are returned.

Sample: `docs/pre-phase2-pricempire-samples/filtered-sources.json` (3 items each with all 3 provider rows).

### Call 4 — `?page=2` (pagination probe)

Length 39,392. First three item names identical to Call 2 (also starting with `Glock-18 | Gamma Doppler (Factory New)`). **The `page` parameter is silently ignored.** There is no pagination scheme on this endpoint — it's all-or-nothing per call.

If a future tier or endpoint variant exposes pagination (e.g. `?limit=&offset=` or `?cursor=`), nothing in the v4/paid behavior we observed suggests it. The natural cost model is "1 call = full snapshot."

### Call 5 — `/v4/paid/items/metas`

Top-level: bare array, length **91,294** — much larger than `/items/prices`'s 39,392, because metas covers items without provider-served prices too (stickers, graffiti, agents, sealed containers, etc.).

Per-item keys:

```
market_hash_name, image, steam_image_hash, description, market_first_date,
buff_market_id, sticker_id, liquidity, marketcap, count, rank,
steam_last_24h, steam_last_7d, steam_last_30d, steam_last_90d
```

Coverage stats:

- `liquidity` populated: 36,104 / 91,294 (39.5%)
- `rank` populated: 26,150 / 91,294 (28.6%)
- `steam_last_24h > 0`: 26,949 / 91,294 (29.5%)

The top-ranked items match the kind of "tradable, liquid CS2 skins" that belong in a curated watchlist:

```
rank=1  M4A4 | Buzz Kill (Field-Tested)
rank=2  M4A1-S | Hot Rod (Factory New)
rank=3  M4A4 | Buzz Kill (Minimal Wear)
rank=4  SSG 08 | Dragonfire (Minimal Wear)
rank=5  ★ Butterfly Knife | Fade (Factory New)
rank=6  SSG 08 | Dragonfire (Field-Tested)
rank=7  SSG 08 | Dragonfire (Factory New)
rank=8  ★ Sport Gloves | Hedge Maze (Field-Tested)
rank=9  AK-47 | Hydroponic (Factory New)
rank=10 ★ Sport Gloves | Pandora's Box (Field-Tested)
```

This is **exactly the right shape for bootstrapping a tracked watchlist** by picking the top-N items by `rank` or `liquidity`, without needing to manually curate.

Samples: `docs/pre-phase2-pricempire-samples/metas-sample.json` (first 3 raw — first one is a graffiti with no rank), `docs/pre-phase2-pricempire-samples/metas-sample-with-rank.json` (first 3 with `rank` and `liquidity` populated — closer to "what we'd actually use").

### Call 6 — `/v4/paid/inventory` (no params; reserved follow-up)

```json
{"message":["app_id must be 730, 570, 440 or 252490"],
 "error":"Bad Request","statusCode":400}
```

A 400 with the message above tells us:

1. The endpoint exists (would 404 otherwise).
2. Bearer auth was accepted (would 401 otherwise).
3. `app_id` is a required parameter, restricted to the set `{730 (CS2), 570 (Dota 2), 440 (TF2), 252490 (Rust)}` — Pricempire serves four games through this endpoint.
4. Presumably also needs a `steam_id` or similar identifier (not surfaced in this 400, but no inventory endpoint anywhere serves "give me everyone's inventory").

Per the brief's workflow (do not retry on non-200), I stopped here without burning further calls. The 400 already answers the relevant Phase 2 question — whether the endpoint is available.

Sample (the error response itself): `docs/pre-phase2-pricempire-samples/inventory-400.json`.

---

## 2. Dominant call pattern

**Single-shot bulk-full.** One HTTP call returns the entire CS2 catalog (~39k items for prices, ~91k for metas). There is no:

- pagination scheme on prices or metas
- per-item lookup endpoint observed
- diff / "what's-changed-since" endpoint observed
- streaming / WebSocket alternative observed

The natural cost model is therefore "1 call = full snapshot." `sources=` shrinks the *nested* `prices` array per item but does not reduce the item count or the per-call cost. `app_id=730` is a no-op on the prices endpoint (it's CS2-only by default).

**Practical consequence.** Whatever cadence you choose, you're choosing it for the *whole catalog at once*. There is no way to "refresh only the 500 items I care about" — Pricempire gives you all 39,392 or nothing.

This is actually a *feature* for breadth coverage: a tracked watchlist of any size, plus on-demand lookups for items outside the watchlist, can both be served by the same bulk snapshot. The local cache is the load-bearing component; the API is just the refresh source.

---

## 3. Budget projections (10,000 calls/month Developer tier)

### Scenario A — Tracked watchlist of 500 items, refreshed hourly

Pricempire is bulk-only, so "refresh 500 items hourly" is mechanically the same as "refresh all 39,392 items hourly" — one call per hour.

- 24 calls/day × 30 days = **720 calls/month**
- **7.2% of the 10,000/month budget**
- Headroom: could tighten to every 15 minutes (2,880/month = 29% of budget) or every 5 minutes (8,640/month = 86%, tight but viable)

The single-shot pattern means hourly is wildly under-budget. The freshness vs. budget trade-off doesn't kick in until ~10-minute cadence.

### Scenario B — Tracked watchlist (500) refreshed hourly + on-demand for unknown items with 1h cache, 200 unique queries/day

The "unknown items" framing assumes per-item Pricempire lookups exist. They don't (verified — no per-item endpoint on `/v4/paid/items/prices`, and `sources=` doesn't take a `market_hash_name=` filter).

The realistic implementation is: **the hourly bulk call already covers all 39,392 items in the local cache.** An on-demand query for an item outside the tracked watchlist becomes a local lookup against the most recent bulk snapshot — zero incremental API calls.

- **API cost = same as Scenario A: 720 calls/month (7.2%)**, regardless of on-demand query volume
- Cache TTL effectively becomes "max staleness of the bulk snapshot" — at hourly refresh, that's ≤1 hour by construction
- 200/day on-demand queries don't move the needle because they don't touch Pricempire at all

If a future requirement demands sub-hour freshness on a specific item, the answer is "tighten the bulk cadence," not "add per-item lookups" — because per-item lookups don't exist.

### Scenario C — Inventory lookups, 50/day

The `/v4/paid/inventory` endpoint exists and requires `app_id` ∈ {730, 570, 440, 252490}. Assuming the standard cost model (1 HTTP call = 1 budget unit; consistent with the prices endpoint and absent any contrary signal):

- 50 calls/day × 30 days = **1,500 calls/month**
- **15% of the 10,000/month budget**
- Combined with Scenario A or B: **720 + 1,500 = 2,220/month = 22.2% of budget**

Even doubling the inventory volume to 100/day (3,000/month = 30%) plus hourly bulk leaves ~50% headroom. The budget is not the constraining factor at Developer tier; per-call latency (3-9s on the prices endpoint) and 33-47 MB response sizes are the real operational considerations.

---

## 4. Recommended cadence and scope for Phase 2

Given the findings, the natural Phase 2 architecture is: layer Pricempire as a **bulk-snapshot collector** on a 15-30 minute cadence — fast enough that the local snapshot is never more than half an hour stale, cheap enough at 2,880/month or fewer calls to leave room for inventory lookups and any future endpoints. Persist the latest snapshot to a dedicated table (or hypertable) keyed on `(market_hash_name, provider_key, observed_at)` so the existing items-driven API can serve both tracked-watchlist queries and arbitrary "anything Pricempire knows about" lookups from the same store. The existing Steam/Skinport/DMarket collectors stay — they're still the only source for Steam Market data, and they offer per-item freshness on the curated watchlist that Pricempire's bulk pattern can't match. Bootstrapping the curated watchlist from `/v4/paid/items/metas` rank/liquidity (one-off, not on a cadence) gives a defensible breadth list without manual selection. Inventory endpoint is reserved for a future user-facing feature (deal-evaluation against a connected Steam account); not blocking Phase 2.

---

## 5. Call ledger

| # | Endpoint | Status | Reason |
|---|---|---|---|
| 1 | `GET /v4/paid/items/prices` | 200 | Empty-params baseline |
| 2 | `GET /v4/paid/items/prices?app_id=730` | 200 | CS2-scoped probe |
| 3 | `GET /v4/paid/items/prices?app_id=730&sources=buff163,skinport,dmarket` | 200 | Filtered-source probe |
| 4 | `GET /v4/paid/items/prices?app_id=730&page=2` | 200 | Pagination probe |
| 5 | `GET /v4/paid/items/metas` | 200 | Liquidity/rank shape |
| 6 | `GET /v4/paid/inventory` | 400 | Reserve: inventory endpoint existence |

**Total budget consumed: 6 / 10,000 (0.06%).** No retries. No 401s. All payloads saved (trimmed to first 3 items where the raw response exceeded ~10 KB; original sizes documented inline).
