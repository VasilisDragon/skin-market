# ADR 037 - Asset-specific pricing feasibility

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 028, ADR 029, ADR 030

## Context

The bot can already return exact asset attributes from public inventory links
and modern inspect links, then compute a market-name baseline from local USD
rows. That baseline deliberately excludes float, paint-seed, sticker, charm,
and collector premiums.

This ADR investigates whether "piece two" can be built: a deterministic,
defensible price estimate for the specific asset rather than only its
`market_hash_name`.

No pricing code, schema, tool, or prompt behavior is changed by this ADR.

## Research Method

The supplied St4ck links were resolved through Steam Community XML profile
lookup. The vanity profile `St4ck` resolved to SteamID64
`76561198023414915`, and Pricempire returned 692 CS2 inventory assets for that
profile. All 11 supplied asset ids were present.

Worked-example attributes and current `market_baseline` values were produced
with the checked-out application code in a one-off Docker Compose run that used
the existing API/database environment. No secrets were printed or copied.

External source references checked:

- Pricempire API docs: <https://pricempire.com/docs>
- CSFloat API docs: <https://docs.csfloat.com/>
- ByMykel CSGO-API docs: <https://bymykel.com/CSGO-API/>
- Skinport REST API docs: <https://docs.skinport.com/items> and
  <https://docs.skinport.com/sales/history>
- CS2BlueGem API docs: <https://app.csgobluegem.com/api/index.html>
- CSDB Case Hardened guide:
  <https://csdb.gg/guides/case-hardened-guide/>
- SteamAnalyst sticker calculator:
  <https://www.steamanalyst.com/tools/sticker-calculator>
- CSBoard Doppler guide:
  <https://www.csboard.com/en/blog/cs2-doppler-phase-guide-prices>

An unauthenticated live request to CSFloat `GET /api/v1/listings` from this
host returned HTTP 403 for representative queries, despite the docs presenting
the all-listings endpoint without an authorization header. Treat CSFloat as
documented-public but operationally unreliable until access is confirmed.

## R.1 Source Survey

| Signal | Obtainability | What it returns | Usefulness | Limitation |
|---|---|---|---|---|
| Pricempire `/v4/paid/inventory` | (c) Account/API-key gated, already present in this stack | Per-owned-asset `float_value`, `paint_seed`, low/high rank fields, stickers, charms, and `market_hash_name` | Strong exact-attribute source for inventory links | It does not provide a verified per-asset sale price or sticker/pattern premium |
| Pricempire item prices/history/comparison | (c) Account/API-key gated, already present in this stack | Market-name price points by provider | Good baseline input | Still grouped by `market_hash_name`; no exact sticker/seed repricing |
| ByMykel CSGO-API | (a) Freely queryable static JSON | Item, sticker, keychain, paint index, wear, and market name metadata | Strong naming/schema source | No price signal |
| Skinport `/v1/items` | (a) Freely queryable, no authorization, Brotli required | Current market-name aggregate prices, quantity, item pages | Market-name liquidity/baseline signal | No exact float, seed, sticker, or charm segmentation |
| Skinport `/v1/sales/history` | (a) Freely queryable, no authorization, Brotli required | Aggregated sold-item min/max/avg/median/volume by market name over 24h/7d/30d/90d | Useful sanity check for market-name baseline | Aggregated by `market_hash_name`; no per-asset attributes in the documented response |
| CSFloat `GET /api/v1/listings` | Docs imply (a), observed as effectively (b)/(c) from this host because requests returned HTTP 403 | Active listing ask prices with `float_value`, `paint_seed`, `paint_index`, stickers, SCM sticker prices, and filters for float/seed/stickers | Potentially the best public exact-attribute comparable-listing source if access is stable | Active asks are not confirmed sales; access needs explicit verification before implementation |
| CSFloat specific listing endpoint | Docs imply (a) for known listing ids | Active or inactive listing details with exact attributes | Useful for operator-supplied listing evidence | Needs known listing id; still not proof of a completed sale |
| CS2BlueGem API | (c) API key required; production data is plan-gated | Case Hardened paint-seed catalog attributes, gem tier, blue percentages, 3-month average sale price by seed/wear, and restricted raw sales dump | Best structured Case Hardened pattern-pricing source found | Requires vendor/API access; raw sales dump is allowlist-only; scoped mostly to Case Hardened/Heat Treated |
| Community pattern guides and calculators | (b) Public HTML, scrapeable but fragile and not authoritative | Human-readable tiers, seed examples, fade/Doppler/sticker heuristics | Useful research and operator review material | Not stable enough for autonomous pricing math without curated fixtures |
| BUFF163 direct marketplace data | (c) Account/region/anti-bot gated for direct access | High-liquidity listings/sales with floats and stickers | Market participants rely on it heavily | Direct autonomous access is outside scope; use only through licensed aggregators already in stack |
| Steam Community Market endpoints | (b) Public but reverse-engineered/rate-limited | Market-name price overview/order book/history | Basic baseline or liquidity check | No exact asset attributes or applied-premium signal |

### Pattern

Pattern-sensitive pricing is not one problem:

- Case Hardened and Heat Treated need item-plus-seed tier data such as blue
  percentage, playside/backside rank, and historical sale comps.
- Fade/Marble Fade need deterministic seed-to-percentage or seed-to-category
  mapping.
- Doppler/Gamma Doppler gem phases are partly already separated in the
  `market_hash_name` in this stack, but standard phase/max-color premiums can
  still depend on paint seed.
- Crimson Web needs web count/placement and float interaction. I found no
  stable non-gated API for web counting.

CS2BlueGem is the only structured pattern-level pricing API found in this
research. It is useful but account/API-key gated, so it cannot be adopted
autonomously under the current boundary.

### Float

The stack already obtains exact float and rank-like fields for inventory links.
CSFloat's documented listing API can filter active listings by `min_float` and
`max_float`, and sort by `lowest_float`, `highest_float`, or `float_rank`.
That would support deterministic comparable-search math if access is stable.

Skinport and Pricempire market-name aggregates cannot isolate float premiums.
They can validate broad market level, not the premium for a 0.004 ruby knife or
a rank-1 Factory New Crimson Web.

### Stickers And Charms

The stack can read applied sticker names and wear from inventory/inspect data.
ByMykel can map sticker ids/names. CSFloat listing docs show sticker objects
with SCM standalone price and sticker filters, but this host could not query
the endpoint successfully without authorization.

No non-gated source found gives a dependable applied-sticker premium for a
specific craft. Public calculators use SP% ranges. Those ranges are useful
for manual explanation but are not a validated estimator. The correct applied
premium depends on sticker, skin, slot, wear/scrape, craft appeal, matching,
and buyer demand.

## R.2 Determinism Check

Deterministic and already buildable:

- Parse inventory/inspect links.
- Resolve vanity profile to SteamID64 when Steam's XML profile surface works.
- Decode or fetch exact `market_hash_name`, float, paint seed, paint id,
  sticker names, sticker wear, charm data, and ranks.
- Compute the existing market-name baseline from local rows.
- Map static ids and names through ByMykel CSGO-API snapshots.

Deterministic only if the source is approved and reachable:

- Filter CSFloat-like active listings by market name, float range, paint seed,
  paint index, and sticker ids, then compute comparable active-ask statistics.
- Query a pattern catalog such as CS2BlueGem for `item_name + paint_seed` and
  consume returned tier/blue-percent fields.
- Apply a pre-registered formula to an operator-approved confirmed-sales
  corpus.

Not deterministic enough without human calibration:

- Deciding that a particular sticker craft deserves 5%, 20%, or 50% sticker
  percentage.
- Pricing a souvenir sticker combination when the market name hides the
  team/player/event details.
- Assigning Crimson Web web-count/placement value without a reliable classifier.
- Treating active listing ask prices as realized value.
- Accepting community guide ranges as a production price.

## R.3 Ground Truth Requirement

A real per-asset repricer cannot be self-verified. It needs confirmed sales
whose exact attributes did not come from the same API being tested.

The operator should supply a corpus with:

- sale price, currency, marketplace, fee handling, and sale timestamp;
- asset id or inspect link;
- exact `market_hash_name`, float, paint seed, paint id, StatTrak/Souvenir flag,
  sticker names, sticker slots, sticker wear, charms, and screenshots when
  pattern placement matters;
- proof of sale, such as marketplace receipt, trade history plus payout, or an
  operator-reviewed seller/buyer record.

Minimum useful corpus:

- 10-20 cases for an initial "explain-only/evidence-only" check.
- 30-50 confirmed sales per narrow model family before returning a numeric
  premium, for example Doppler gem low-float knives or one specific sticker
  craft family.
- 100-200+ confirmed sales for broad sticker pricing or Case Hardened
  cross-weapon tier pricing, plus holdout examples that are never used while
  tuning.

Do not scrape or invent "confirmed" sales. Listing pages, guide articles, and
market-name aggregates are evidence for context, not validation truth.

## R.4 Worked Examples

All supplied links resolved and all assets were found. Current baselines are
from local rows observed on 2026-05-23.

| Asset id | Current asset | Exact attributes | Current market baseline | Real price drivers | Obtainable signal found | Research-only sketch |
|---|---|---|---|---|---|---|
| `50929830951` | `StatTrak™ M4A4 \| Howl (Factory New)` | Float `0.059376537799835205`, seed `447`, four `Sticker \| Titan (Holo) \| Katowice 2014`, no sticker wear | $11,115.69-$17,769.69, mid $15,282.80, high confidence | Four Titan Kato 2014 holos dominate; StatTrak/FN Howl liquidity; sticker position and craft desirability | Exact stickers are obtainable; public SP% heuristics exist; no confirmed applied-premium source found | A real estimate would be base Howl baseline plus applied-sticker premium. The premium cannot be chosen defensibly without confirmed sales for comparable 4x Titan Holo Howls |
| `50927163695` | `★ Sport Gloves \| Nocts (Factory New)` | Float `0.06413953006267548`, seed `47`, no stickers/charms | $1,856.05-$3,377.49, mid $2,976.40, high confidence | Float within FN band, glove wear appearance, liquidity | Exact float obtainable; Skinport/Pricempire market-name aggregates obtainable; CSFloat active comps documented but observed 403 | Float-aware estimate would compare active/sold Nocts FN near `0.064`; no confirmed per-float sales source was available |
| `50720169688` | `Souvenir AWP \| Dragon Lore (Factory New)` | Float `0.06906184554100037`, seed `45`, Cologne 2015 ESL/TaZ/Virtus.Pro/Team Immunity gold stickers | $1,030.32-$515,158.23, mid $258,094.28, medium confidence | Souvenir sticker set, teams/player/event, FN float near upper FN limit, Dragon Lore liquidity | Exact souvenir stickers obtainable; market-name baseline is extremely wide; no applied souvenir-combo pricing source found | The current baseline already shows market-name instability. Any specific price needs confirmed Souvenir Dragon Lore sales with comparable 2015 sticker sets |
| `30373291232` | `Souvenir AWP \| Dragon Lore (Battle-Scarred)` | Float `0.5557667016983032`, seed `629`, Cologne 2015 Na'Vi/ESL/CLG/tarik gold stickers | $1,471.89-$34,525.74, mid $17,998.82, medium confidence | Souvenir sticker set, Battle-Scarred float, teams/player/event | Exact attributes obtainable; no per-combo confirmed sales source found | Similar to the FN Souvenir Dragon Lore: market-name baseline is a context range, not an appraised value |
| `29799354226` | `★ StatTrak™ Huntsman Knife \| Doppler (Factory New) - Ruby` | Float `0.004588412120938301`, seed `624`, paint id `415`, low/high ranks `3`/`18` | No local USD rows | Ruby phase is already in market name; very low float/rank may matter | Exact rank/float obtainable; CSFloat comps would be useful if access works | A float-aware comp could compare ST Huntsman Ruby listings/sales below `0.005`; current stack has no baseline to adjust |
| `29424312578` | `★ StatTrak™ Karambit \| Doppler (Factory New) - Ruby` | Float `0.006441197823733091`, seed `400`, paint id `415`, low/high ranks `3`/`14` | $5,232.56-$8,831.17, mid $8,400.20, high confidence | Ruby phase, Karambit model, StatTrak rarity, low float, corner/wear appearance | Phase and low float obtainable; active comps source not confirmed reachable | Estimate should start from Ruby market-name comps and then compare nearby low-float ST Karambit Rubies; no verified sales corpus is available |
| `29424312895` | `★ StatTrak™ Karambit \| Doppler (Factory New) - Sapphire` | Float `0.01709936559200287`, seed `367`, paint id `416` | $3,024.73-$5,150.00, mid $4,733.53, high confidence | Sapphire phase, Karambit model, StatTrak rarity, float/corner appearance | Phase is market-name-separated; exact float obtainable | Same method as Ruby, but Sapphire comps. Current baseline is useful context, not a final appraisal |
| `29424312302` | `★ StatTrak™ Bayonet \| Doppler (Factory New) - Sapphire` | Float `0.007549697998911142`, seed `909`, paint id `416`, low/high ranks `2`/`6` | No local USD rows | Sapphire phase, Bayonet model, StatTrak rarity, low float/rank | Exact attrs obtainable; no local baseline | Needs external comps or confirmed sales before a numeric estimate can be returned |
| `29424310215` | `★ StatTrak™ Butterfly Knife \| Crimson Web (Factory New)` | Float `0.06518891453742981`, seed `402`, paint id `12` | No local USD rows | Web count/placement, FN float, Butterfly model, StatTrak rarity | Exact seed/float obtainable; no stable public web-count classifier found | Do not price yet. A web-count classifier or operator-confirmed pattern tier is required before comps are meaningful |
| `29424308892` | `★ StatTrak™ Karambit \| Doppler (Factory New) - Black Pearl` | Float `0.03176920488476753`, seed `344`, paint id `417` | $4,938.18-$9,699.98, mid $9,272.89, high confidence | Black Pearl phase, Karambit model, StatTrak rarity, float/corner appearance | Phase is market-name-separated; exact float obtainable | Could be a narrower phase/float comparable model after CSFloat or confirmed-sale access is approved |
| `29424309983` | `★ StatTrak™ Karambit \| Crimson Web (Factory New)` | Float `0.06860896944999695`, seed `323`, low/high ranks `1`/`1` | No local USD rows | Web count/placement, rank, FN float, Karambit model, StatTrak rarity | Exact rank/seed/float obtainable; no web-count price source found | This is likely a high-value special case, but the rank cannot be translated into dollars without verified Crimson Web sales or an approved classifier/source |

## R.5 Recommendation

Do not build a broad per-asset repricer yet.

The next implementation, when a future goal allows code, should be an
evidence collector rather than a price oracle:

1. Add a deterministic "premium evidence" layer that shows exact attributes,
   current market-name baseline, source availability, and comparable-listing
   queries where approved access works.
2. Add source adapters only after explicit operator approval for each gated
   provider. CSFloat access must be tested from deployment, and CS2BlueGem would
   require a vendor API key and plan decision.
3. Require an operator-supplied confirmed-sales corpus before any numeric
   premium is returned.
4. Start with narrow families:
   - Doppler/Gamma Doppler gem variants where the phase is already in
     `market_hash_name`, and the remaining adjustment is mostly float/rank and
     model liquidity.
   - Low-float/rank explanations for items where the market name already
     captures most of the variant.
   - Case Hardened only if CS2BlueGem or an operator-curated pattern table is
     approved.
   - Stickers only as "premium likely/not priced" until confirmed applied-sale
     data exists.
5. Keep bot wording honest: "market baseline plus premium evidence", not
   "appraised value", until holdout sales validate the model.

The clearest near-term product value is not a single price number. It is a
collector-risk report that says why the baseline is incomplete, which premium
signals are present, which sources were checked, and what evidence is missing
before a real appraisal can be trusted.
