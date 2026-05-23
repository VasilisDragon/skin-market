# ADR 028 — Public-inventory asset valuation

**Status:** Accepted; Phase A fixture gate satisfied by independent fixtures  
**Date:** 2026-05-23  
**Related:** ADR 014 (read API), ADR 016 (bot runtime), ADR 024
(tier architecture), ADR 026 (DeepSeek cutover), ADR 027 (cost control)

## Context

Phase A adds a Discord flow where a user pastes a Steam public-inventory
item link and gets a value gauge for that exact asset. The key distinction from
the existing `/items/{slug}/price` flow is that the input identifies an asset
id, not just a `market_hash_name`.

The deterministic-core rule still applies:

- The LLM may choose the tool and render the response.
- The LLM must not parse Steam links, fetch inventories, match asset ids, or
  compute valuations.
- Secrets remain environment variables and are never logged or stored.

Pricempire's live public docs list:

```
GET https://api.pricempire.com/v4/paid/inventory
```

with required `steam_id` and `app_id`, optional `force`, and CS2 app id `730`.
The example response includes top-level `items[]`; each asset carries
`asset_id`, `d`, `low_rank`, `high_rank`, `float_value`, `paint_seed`,
`stickers`, `charms`, and nested `item.paint_id` plus
`item.market_hash_name`.

Source: <https://pricempire.com/docs>

## Verified Data Path

Steam inventory item links encode the app, context, and asset id in the URL
fragment:

```
https://steamcommunity.com/profiles/<steamid64>/inventory/#730_2_<asset_id>
https://steamcommunity.com/id/<vanity>/inventory/#730_2_<asset_id>
```

For numeric profile links, the SteamID64 is already present. For vanity links,
the API resolves SteamID64 through Steam Community's public XML profile surface
before calling Pricempire.

The implemented flow is:

1. Parse and validate the Steam URL. Only `steamcommunity.com` inventory links
   with `#730_2_<asset_id>` are accepted.
2. Resolve SteamID64 if the URL uses `/id/<vanity>/`.
3. Call Pricempire `/v4/paid/inventory?steam_id=<id>&app_id=730`.
4. Find the asset whose `asset_id` matches the URL fragment.
5. Read exact asset attributes from that asset row.
6. Compute a baseline USD range from local latest USD market rows for the
   asset's `market_hash_name`.

A live dry run on 2026-05-23 used an existing DMarket fixture-style public
inventory link:

```
https://steamcommunity.com/profiles/76561199276192848/inventory/#730_2_51590003382
```

Pricempire returned a matching asset:

- `market_hash_name`: `Souvenir MP9 | Hot Rod (Factory New)`
- `float_value`: `0.035739749670028687`
- `paint_seed`: `169`
- `paint_id`: `33`
- `stickers`: 4

The local baseline gauge used six USD Pricempire-backed rows and excluded Steam
Wallet credit.

## Decision

Add a protected read-API route:

```
POST /asset-valuations/inventory
```

with body:

```json
{"inventory_url": "https://steamcommunity.com/profiles/.../inventory/#730_2_..."}
```

The API, not the LLM, performs the deterministic work. The Discord bot gets a
thin `value_inventory_item` wrapper that calls this local route, matching the
existing bot-tool pattern.

The API service now receives `PRICEMPIRE_API_KEY` through Docker Compose. Missing
or rejected keys are operator/configuration errors. User-input failures return a
structured `status="unreadable"` result so the bot can respond plainly.

The first valuation method is intentionally conservative:

- Use latest local USD rows for the asset's `market_hash_name`.
- Include direct USD rows and Pricempire USD sub-provider rows.
- Exclude Steam Wallet credit from the USD gauge.
- Report `low`, `mid` (median), `high`, source count, and confidence.
- Surface `float_value`, `paint_seed`, `paint_id`, ranks, stickers, and charms
  as exact attributes.
- Do not apply sticker, charm, float, or pattern premiums until the
  independently researched known-answer fixture calibrates those cases.

## Known-answer fixture

The Phase A fixture uses DMarket response fixtures as the independent evidence
source. Those fixture rows include Steam inventory item URLs, DMarket listing
prices, exact float values, paint seeds, paint indexes, and sticker names. The
tests assert that `tests/fixtures/inventory_known_answers.json` still matches
the referenced DMarket fixture rows before exercising the valuation route.

Three cases are pinned:

| Item | Source evidence | Expected value | Tolerance | Notes |
|---|---:|---:|---:|---|
| `Souvenir MP9 \| Hot Rod (Factory New)` | `tests/fixtures/dmarket/mp9-hot-rod-factory-new.json` object 0 | `$228.27` | 30% | Souvenir item with four gold Boston 2018 stickers; live Pricempire lookup reproduced float `0.035739749670028687`, seed `169`, paint id `33`, and all four sticker names. |
| `StatTrak™ M4A1-S \| Cyrex (Field-Tested)` | `tests/fixtures/dmarket/m4a1-s-cyrex-field-tested.json` object 0 | `$166.96` | 30% | StatTrak rifle with two foil stickers; live Pricempire lookup reproduced float `0.20269429683685303`, seed `712`, paint id `360`, and both sticker names. |
| `Desert Eagle \| Oxide Blaze (Factory New)` | `tests/fixtures/dmarket/desert-eagle-blaze-factory-new.json` object 4 | `$1.58` | 30% | Low-value liquid skin canary; live Pricempire lookup reproduced float `0.06637155264616013`, seed `520`, paint id `645`, and zero stickers. |

The 30% tolerance is deliberately broad because the current gauge is a
market-name baseline from local USD sources, not a listing-specific repricer.
The exact-asset attributes are checked exactly; the value check prevents the
baseline from drifting into the wrong order of magnitude while avoiding a false
promise about sticker/pattern premiums.

## Private And Unresolvable Handling

Invalid links, private profiles, unreadable inventories, and asset-id misses do
not fall back to a `market_hash_name` average silently. They return:

```json
{
  "status": "unreadable",
  "reason": "...",
  "message": "..."
}
```

The bot prompt instructs the model to render this as a private/unreadable
inventory response and stop.

## Rejected

- **Bot calls Pricempire directly.** Rejected to preserve the existing bot shape:
  bot tools are wrappers over the local API, and market/data fetching remains
  deterministic Python outside the LLM.
- **Store inventory snapshots.** Rejected for Phase A. The feature needs a live
  per-request read; storing full user inventories would expand privacy and data
  retention scope without a requirement.
- **Invent sticker or pattern premiums.** Rejected. The inventory endpoint gives
  attributes, not a verified per-asset price. Premium modeling needs
  stronger fixture coverage before certification.
- **Use CSFloat/account-gated data.** Rejected by the session boundary. Any path
  requiring a CSFloat account, Steam 2FA, or prior CSFloat trade history remains
  out of scope.

## Gate

GATE A-FIXTURE is satisfied for Phase A by
`tests/fixtures/inventory_known_answers.json` and
`tests/test_asset_valuation.py`. The fixture verifies independently sourced
attributes and values, then checks the API result's exact attributes and gauge
tolerance.
