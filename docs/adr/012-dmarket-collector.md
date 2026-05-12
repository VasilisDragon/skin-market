# ADR 012 — DMarket collector: second real-money source

**Status:** Accepted
**Date:** 2026-05-12
**Related:** docs/sources-and-semantics.md, ADR 006 (collector resilience),
ADR 008 (Skinport collector), ADR 010 (analytics design)

## Context

`docs/sources-and-semantics.md` committed the project to a "three or
more sources" triangulation goal. With Steam Market (wallet-credit) and
Skinport (USD), we had two — enough to *detect* disagreement, not
enough to *resolve* it, because the wallet-credit-vs-USD denomination
gap makes absolute comparison meaningless.

DMarket adds the third source. It's:

- **Real-money USD settlement**, same denomination class as Skinport.
  A Skinport-vs-DMarket spread is unambiguous price signal — both
  sides are real money, so divergence between them is a market fact,
  not a denomination artifact.
- **Per-item, like Steam.** No bulk endpoint; the natural unit is
  ``collect_one(name)`` and the default ``collect_cycle`` from
  ``collectors/base.py`` handles the iteration.
- **Unauthenticated read access.** No API key, no signing dance. The
  public market endpoint at ``GET /exchange/v1/market/items`` works
  with just query params.
- **US-leaning operator**, complementary to Skinport (EU-leaning) for
  v3+ regional-arbitrage signals.

## Decisions

### 1. Use `.price.USD`, NOT `.suggestedPrice`

DMarket's API returns two price-like fields per offer:

- ``price.USD``: the actual current listing price for this offer.
- ``suggestedPrice.USD``: DMarket's *recommendation* for what the
  seller could/should ask. NOT the listing price; an opinionated
  number computed by DMarket's own pricing engine.

Using ``suggestedPrice`` would silently substitute DMarket's pricing
model for actual market data — a quietly destructive bug that's
hard to spot in cycle logs because the numbers still look plausible.

The collector reads ``objects[0].price.USD`` exclusively. A test
fixture in ``tests/test_dmarket_collector.py`` plants
``suggestedPrice = 10×price`` on every offer, so any future code path
that accidentally reads the wrong field fails the test loudly.

### 2. Price format: stringified integer cents

``price.USD`` is a **string** containing an **integer number of cents**.
``"4378"`` means $43.78. ``parse_dmarket_price`` reconstructs the
``Decimal`` via string-insertion of the decimal point:

```python
s = f"{cents:03d}"               # "4378" or "001" or "100"
return Decimal(f"{s[:-2]}.{s[-2:]}")
```

NOT via ``Decimal(cents) / Decimal(100)``. Both are numerically correct
but Decimal-division strips trailing zeros (``Decimal(100) / Decimal(100)``
returns ``Decimal('1')``, not ``Decimal('1.00')``), which is fine for
storage but breaks test equality assertions that expect the cent-level
representation. The string-insertion path preserves ``X.YY``.

### 3. Pacing: 3 seconds per item, 15-minute cycles

- ``inter_request_delay = 3.0`` — DMarket is permissive but has no
  documented rate limit, so we stay conservative-but-not-paranoid.
  Slightly faster than Steam's 5s pacing because DMarket has no
  Wallet-cookie footgun.
- Cycle interval: 15 minutes (vs Steam's 30 min, vs Skinport's 5 min).
  At 48 items × 3s pacing, one DMarket cycle takes ~2.5 minutes —
  comfortable inside the 15-minute interval with plenty of headroom
  for retry backoffs.

### 4. ``volume`` semantics: stock, like Skinport

``len(objects)`` (count of currently-listed offers, capped at
``limit=100``) goes into ``prices.volume``. This is a **stock**
measurement (current listings count), the same kind of signal as
Skinport's ``quantity``. NOT comparable to Steam's ``volume`` (24-hour
sales count, a flow). Analytics SQL already filters by source when
comparing volume — see ADR 010 §1.

When the watchlist grows past ~100 listings per item (unlikely for
the high-tier items we track), we'd raise ``limit`` and re-evaluate.
For v1 the cap is fine; ``raw_response.total_objects_returned`` is the
same number for audit trail.

### 5. Float-tier metadata preserved but not promoted

DMarket's response includes ``extra.floatValue`` per offer — the
specific float for the listed item (v2 will use this for
float-tier-accurate pricing per the PROJECT_SPEC v2 roadmap). For
v1, we persist the cheapest offer's full dict in ``raw_response``,
so the float is available for replay/analysis without a top-level
column.

Promoting it to a column would be premature: the v2 float-tier
architecture might want a separate ``offers`` table (per-listing,
not per-cycle-snapshot), at which point a column on ``prices``
becomes the wrong shape.

### 6. ``Empty objects[]`` = "unavailable", not "parse failure"

Same distinction as ADR 010 §6 for Steam's ``success:false`` /
Skinport's ``min_price:null``: an empty ``objects`` array means "no
current listings on DMarket right now", which is real rarity signal,
not a collector failure. The cycle counter is ``unavailable``, not
``lookup_failed``; the log is INFO, not WARNING.

## Consequences

- **Pro:** Skinport-vs-DMarket is now a real-money pair. Cross-source
  divergence between them is unambiguous price signal (e.g. a
  Doppler price jump that hits both → variant-mix shift across
  marketplaces; one but not the other → arbitrage opportunity or
  marketplace-specific liquidity event).
- **Pro:** Analytics needs no changes. ``cross_source_view`` already
  iterates all enabled sources; with DMarket in ``sources``, the
  view automatically grows from 2 entries to 3, and the spread
  insight grows from 1 row per item to ``C(3, 2) = 3`` rows per item.
  Phase 5's source-dynamic SQL pays off here — see ADR 010 §1.
- **Pro:** No auth, no API key rotation, no cookie strategy. DMarket's
  public endpoint is the most operationally friendly source we have.
- **Con:** Per-item polling on a third source roughly doubles the
  total HTTP load (Steam + DMarket are both per-item; Skinport is
  one bulk call). Still well under any practical limit.
- **Con:** DMarket's unauthenticated endpoint isn't *promised* to
  remain unauthenticated. If they ever require keys, the collector
  module gains a ``DMARKET_API_KEY`` env-var path (similar to the
  cookie escalation plan for Steam in ADR 006 §4).
- **Related:** the bot's reply formatting (Phase 7) should render the
  full 3-source view honestly:
  ``$X.XX USD on Skinport / $Y.YY USD on DMarket / $Z.ZZ wallet credit on Steam``.
  Skinport-vs-DMarket disagreement is now meaningful in absolute terms
  for the first time.
