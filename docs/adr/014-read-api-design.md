# ADR 014 — Read API design

**Status:** Accepted
**Date:** 2026-05-12
**Related:** ADR 002 (TimescaleDB), ADR 003 (FastAPI), ADR 010 (analytics), ADR 013 (rate-limit policy), `docs/sources-and-semantics.md`

## Context

Phase 6 introduces `api`, a read-only FastAPI service that the bot (Phase 7) and any future SaaS consumers will hit for prices, history, insights, charts, and deal evaluation. The architecture's load-bearing principle — sources are denominated differently and the system never collapses across them — must be enforced at the wire, not just internally. This ADR pins the design decisions that make the API faithful to that principle.

## Decisions

### 1. Money is a string in JSON; Decimal in Python

Every price-bearing field — `price`, `current`, `delta`, `offer.amount`, `value` — is typed as `Decimal` in Pydantic models and serialized as a string at JSON time:

```python
from typing import Annotated
from decimal import Decimal
from pydantic import PlainSerializer

MoneyStr = Annotated[Decimal, PlainSerializer(str, return_type=str)]
```

**Why not float.** `NUMERIC(12, 2)` in Postgres is precise on purpose: prices have cents. `Decimal` round-trips through `str()`; a `float` round-trip silently corrupts the last digit (`Decimal(42.50) → "42.500000000000007..."`). Serializing as a float in JSON loses that precision at the boundary, which then writes back into a Decimal column as 18 ulps of garbage. The bug shape is well known and we've already designed it out of the collector (ADR 008 §2's note on `Decimal(str(x))` vs `Decimal(x)`); the API boundary gets the same discipline.

**Why Annotated PlainSerializer rather than `model_config = {"json_encoders": ...}`.** That dict is deprecated in Pydantic v2. The `Annotated[Decimal, PlainSerializer(str)]` alias is the v2 idiom, declarative and per-field. One alias (`MoneyStr`) used everywhere money appears, no per-model decorator boilerplate.

### 2. Every price field is paired with `denomination`

There is no top-level scalar `price` in any API response. The `/price` endpoint returns a list of `(source, denomination, price, volume, observed_at)` tuples. Even with one source enabled, the response shape is the same — a list. Bots that render this MUST consume the `denomination` tag or the system trusts the architecture's invariant from `docs/sources-and-semantics.md`.

This is enforced by the schema (`PerSourcePrice` requires `denomination: Literal["usd", "wallet_credit"]`) and by the integration tests (`test_multi_source_shape_with_denominations` asserts no top-level `price` key and a denomination per source).

Adding a fourth source remains a config change: insert the row in `sources` with the right `denomination`, implement the collector (Phase 4 pattern), and the API surfaces it dynamically. The `WHERE s.enabled = TRUE` filter the analytics + scheduler already respect carries over here.

### 3. No auth in v1 — explicit dependency on the bot-in-compose deployment

The api service in `docker-compose.yml` has no `ports:` mapping. Other services (collector, analytics, the eventual bot) reach it at `http://api:8000` via the compose internal network. The host can't see it; the public internet can't see it.

**This is the gatekeeper.** Auth is not added in v1.

**Explicit dependency:** if Phase 7 (or any later phase) deploys the bot as a separate process on the Spark host, or on a different machine, the network-as-gatekeeper assumption stops holding. The remediation is a single FastAPI dependency:

```python
SKIN_MARKET_API_TOKEN = os.environ["SKIN_MARKET_API_TOKEN"]

def require_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = f"Bearer {SKIN_MARKET_API_TOKEN}"
    if not secrets.compare_digest(authorization or "", expected):
        raise HTTPException(status_code=401)

# applied via app.include_router(items.router, dependencies=[Depends(require_token)])
```

~15 lines, single env var, no changes to the route handlers. If you find yourself deploying the bot out-of-compose, this is the first thing to add, before opening the port. ARCHITECTURE.md's "user accounts / auth (v5+)" applies to user-tier auth (login, multi-tenant); a single static bearer for machine-to-machine is a different concern, deliberately deferred but trivially un-deferrable.

### 4. `/deals/evaluate` — currency drives comparable; freshness gates verdict

Request:

```json
{
  "slug": "ak-47-redline-field-tested",
  "offer": {"amount": "42.50", "currency": "usd"}
}
```

Two-stage source classification:

1. **Currency split** (denomination match): sources whose `denomination == offer.currency` are candidates for *comparable*. The others land in *informational* with `reason: "denomination_mismatch"` and a human-readable note explaining why they can't anchor the verdict (Steam wallet credit ≠ USD; ADR 013 §1 and `docs/sources-and-semantics.md`).
2. **Freshness gate**: candidate comparables whose latest observation is older than `COMPARABLE_FRESHNESS_HOURS` (default **4 hours**) are demoted to *informational* with `reason: "stale"`. The verdict math reads only fresh, denomination-matching sources.

Verdict math compares `offer.amount` to `min(comparable.current)` (the cheapest available listing in the offer's currency):

- `below_market`: `offer < cheapest × (1 − AT_MARKET_TOLERANCE_PCT)`
- `at_market`:    within `±AT_MARKET_TOLERANCE_PCT` of cheapest
- `above_market`: `offer > cheapest × (1 + AT_MARKET_TOLERANCE_PCT)`
- `no_comparable_data`: no comparable rows survived the two filters; informational is still returned for context.

`AT_MARKET_TOLERANCE_PCT` is **5%** (`Decimal("0.05")`), `COMPARABLE_FRESHNESS_HOURS` is **4** — both are named module constants in `api/routes/deals.py`. Change the constant, change this ADR alongside. **5%** was picked as a defensible noise floor for spreads across Skinport/DMarket; **4 hours** sits at 4–16× the post-ADR-013 healthy cycle intervals (Steam 60min, Skinport/DMarket 15min), so a single missed cycle does not degrade the verdict but a multi-cycle outage does.

`min(comparable.current)` (cheapest) anchors the verdict because the buyer's natural question is "could I do better elsewhere?". Symmetric for sellers: "should I list at this?" is the same comparison with the same labels.

### 5. `/items/{slug}/insights` excludes `daily_narrative`

`analytics.narrative.generate_and_store` inserts the daily-narrative row with `item_id = (SELECT id FROM items ORDER BY created_at ASC LIMIT 1)` — i.e. pinned to whichever item was inserted first. The narrative isn't about that item; it's a global recap. Exposing it via a per-item endpoint would either lie ("here's the narrative for AK Redline") or be inconsistent (it only appears under one arbitrary slug).

For v1, `/items/{slug}/insights` filters `WHERE insight_type != 'daily_narrative'`. A separate path for the daily narrative is Phase 7 work — likely `/insights/narrative/latest` or a bot-skill tool that knows to bypass the per-item endpoint.

The `DISTINCT ON (insight_type, meta_signature)` SQL (with `meta_signature = COALESCE(meta_info->>'source_id', source_a_id||'/'||source_b_id, '')`) returns the latest row per `(type, sub-key)` combination, so each (moving_avg_7d, source) pair and each (cross_source_spread, source_pair) row is distinct. Item-level insights (cross_source_view, volume_anomaly, cross_source_divergence) use the empty sub-key, which is fine — at most one row per type per item.

### 6. Chart endpoint returns raw PNG

`/items/{slug}/chart` returns `Response(content=png_bytes, media_type="image/png")` — raw bytes, not JSON. Discord renders PNG attachments inline; the bot can write the response body straight to an attachment without base64/JSON unwrap.

Alternative considered: JSON `{"image": "data:image/png;base64,..."}`. Rejected as one more layer of envelope on both sides for no win. matplotlib is imported **inside the route**, not at module level, because it's ~80MB and adds ~600ms to interpreter startup; cold-start matters when the api container is restarted and the bot's first request races the readiness probe.

Charts are **single-source by design**. Y-axis is `Price (USD)` or `Price (Steam Wallet credit)` — a chart spanning denominations would lie about its own axis. The bot's eventual response for "show me X's history" picks one source and labels it; cross-source view is `/items/{slug}/price` (snapshot) or `/items/{slug}/insights` (analytics-derived spread time-series), not a chart.

### 7. `/items/{slug}/history` — bounded defaults, hard cap

Defaults applied when query params are absent:

- `since` = now − 7 days (`HISTORY_DEFAULT_DAYS`)
- `limit` = 500 (`HISTORY_DEFAULT_LIMIT`)
- `limit` hard cap = 5000 (`HISTORY_MAX_LIMIT`), enforced by Pydantic `Query(le=...)` (422 on overflow).

At Skinport's 15-min cadence post-ADR-013, an active item accumulates ~96 observations/day even after dedup; 7 days × 3 sources × an active item easily hits ~2k rows. A "give me all of it" query without bounds would return ~10MB of JSON months from now. Both defaults are documented in the OpenAPI example so callers reading `/docs` know what they get if they pass nothing.

A `since > until` query returns 400 (caller error, not a 500); an unknown slug returns 404 (handled by checking item existence when the result set is empty).

### 8. OpenAPI examples — selective, only where shape carries weight

Three responses ship with `model_config.json_schema_extra.examples`:

- `PriceResponse` — concretely shows the multi-source list with denomination tags and `observed_at`. The architectural invariant is hard to miss when the example is in `/docs`.
- `HistoryResponse` — shows the windowed list with the actual default values baked into the example (`limit: 500`, `since: now-7d`).
- `DealEvaluateRequest` / `DealEvaluateResponse` — the currency semantics and the comparable/informational split need concrete shape.

Skipped: `/items` (list, obvious), `/items/{slug}` (metadata, obvious), `/chart` (PNG, not JSON). A test guards the substantive ones (`test_openapi_includes_examples_on_substantive_endpoints`) so a future refactor can't quietly drop them.

### 9. Test infrastructure: sentinel item + `_preserve_source_enabled_flags`

API tests follow the same sentinel-item pattern as `test_analytics.py` and `test_watchlist_edit.py`:

- A `sentinel_item` fixture inserts a uniquely-named item, runs the test, cleans up its prices/insights/row afterward. The name is `__APITest__ | Sentinel (Field-Tested)` — can't collide with real CS2 data.
- An autouse `_preserve_source_enabled_flags` fixture snapshots `sources.enabled` for every test and restores on teardown, so a test that flips `enabled` (e.g. testing the `WHERE s.enabled = TRUE` filter on `/price`) does not unwind operator state. Same pattern as `test_watchlist_edit.py` (ADR 013 fire-drill side-effect lesson).
- `fastapi.testclient.TestClient` against the real DB — no in-memory fake. Same skip pattern as the rest of the suite (`@_db_required` when `DATABASE_URL` unset or postgres unreachable).

## Consequences

- **Pro:** the architectural invariant ("never collapse across denominations") is now machine-enforced at the API boundary. A bot or a future SaaS consumer cannot accidentally render `$42 on Steam` as USD; the denomination tag is mandatory on every price row.
- **Pro:** money precision survives the wire. `Decimal("42.50") → "42.50" → Decimal("42.50")` round-trips cleanly; no float corruption.
- **Pro:** the api service is one config-change away from third-party consumption — when Phase 7 or later opens the door, the auth dependency is documented and ~15 lines.
- **Con:** `/deals/evaluate`'s 5% / 4-hour constants are static. As the volatility of CS2 prices changes and new sources land, these may need tuning. Named module constants mean tuning is a one-line change + ADR update, not a refactor.
- **Con:** `daily_narrative` is omitted from `/items/{slug}/insights`, which means the bot needs a separate path to fetch it. Phase 7 adds that path; documented here so the omission is intentional, not an oversight.
- **Con:** matplotlib's lazy import inside the chart handler means the first `/chart` request after process start is ~600ms slower than subsequent ones. Acceptable — `/chart` is a low-frequency endpoint, and bot users won't notice on a single request.
