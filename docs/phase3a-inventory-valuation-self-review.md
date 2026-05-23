# Phase 3A inventory valuation self-review

**Date:** 2026-05-23  
**Gate:** GATE 1, after Phase A public-inventory valuation  
**Decision:** Clean. Proceed to Phase B research.

## Phase A commits reviewed

- `5e9a60d feat(asset): value public inventory items`
- `8b091b4 chore(gitignore): ignore env backup files`
- `16b8cff docs(asset): align valuation fixture wording`
- `ee1976d fix(bot): constrain inventory valuation rendering`
- `437f87c fix(bot): suppress inventory source dumps`

## Verified data path

ADR 028 records the live Pricempire inventory path:

1. Parse a Steam Community inventory URL with fragment
   `#730_2_<asset_id>`.
2. Resolve the SteamID64 from `/profiles/<steamid64>/` directly, or from
   `/id/<vanity>/` through the public Steam Community XML profile surface.
3. Call Pricempire `GET /v4/paid/inventory?steam_id=<id>&app_id=730`.
4. Find the matching `asset_id`.
5. Extract exact per-asset attributes from Pricempire: market hash name,
   float, paint seed, paint id, ranks, stickers, and charms.
6. Compute the value gauge from local latest USD market rows for that market
   hash name, excluding Steam Wallet credit.

The deterministic boundary is preserved: the LLM only selects
`value_inventory_item` and phrases the structured tool result. Link parsing,
inventory fetching, asset matching, and value math live in Python.

## Private and unreadable behavior

Invalid links, private profiles, unreadable inventories, and asset-id misses
return structured `status="unreadable"` responses instead of falling back to a
market-name average. The bot prompt tells the model to render the decline and
stop.

Live invalid-app sample after the final prompt fix:

```text
That link is for a Dota 2 inventory item (#570), not CS2 (#730). I can only
value CS2 inventory items.
```

## Fixture evidence

The Phase A known-answer fixture is
`tests/fixtures/inventory_known_answers.json`. It uses DMarket fixture rows as
independent evidence, not the Pricempire inventory response being tested.

Pinned cases:

| Item | Independent source | Exact attributes checked | Value expectation |
|---|---|---|---|
| Souvenir MP9 \| Hot Rod (Factory New) | `tests/fixtures/dmarket/mp9-hot-rod-factory-new.json` object 0 | float `0.035739749670028687`, seed `169`, paint id `33`, four gold Boston 2018 stickers | `$228.27` within 30% |
| StatTrak M4A1-S \| Cyrex (Field-Tested) | `tests/fixtures/dmarket/m4a1-s-cyrex-field-tested.json` object 0 | float `0.20269429683685303`, seed `712`, paint id `360`, two foil stickers | `$166.96` within 30% |
| Desert Eagle \| Oxide Blaze (Factory New) | `tests/fixtures/dmarket/desert-eagle-blaze-factory-new.json` object 4 | float `0.06637155264616013`, seed `520`, paint id `645`, zero stickers | `$1.58` within 30% |

The route fixture tests mock Pricempire and local price points, assert exact
asset attributes, and assert the computed gauge remains inside the independent
tolerance band.

## Verification

Required pre-commit checks passed after the final Phase A prompt fix:

```text
uv run ruff check .
All checks passed!

uv run pytest
518 passed, 1 deselected, 1 warning in 10.58s
```

The bot and API were rebuilt with:

```text
docker compose up -d --build bot
```

Compose reported API healthy and bot started.

## Tool-routing and cost impact

Stable prompt/tool prefix size before Phase A (`fa01737`) versus current
(`437f87c`):

| Version | Tools | Prompt chars | Tool JSON chars | Stable prefix chars | Approx tokens |
|---|---:|---:|---:|---:|---:|
| Before Phase A | 8 | 4,733 | 3,048 | 7,781 | 1,945 |
| Current | 9 | 5,268 | 3,536 | 8,804 | 2,201 |

Net impact: one new tool, `+1,023` stable-prefix chars, approximately `+256`
input tokens before cache effects.

Live `llm_usage_log` rows for the final valid inventory sample used two
DeepSeek calls:

| Call | Prompt tokens | Completion tokens | Cache hit | Cache miss | Cost |
|---|---:|---:|---:|---:|---:|
| Tool-selection call | 2,459 | 80 | 512 | 1,947 | `$0.00029641` |
| Final-render call | 3,402 | 211 | 2,432 | 970 | `$0.00020169` |

Total final valid-sample cost: `$0.00049810`.

## Live quality samples

Valid public CS2 inventory link:

```text
Here's the valuation for your Souvenir MP9 | Hot Rod (Factory New):

Asset Details
- Float: 0.0357 (Factory New)
- Paint Seed: 169
- Stickers: 4x Gold Boston 2018 stickers (ELEAGUE, Cloud9, Skadoodle,
  Virtus.Pro)

Value Gauge (USD)
- Low: $150.13
- Mid: $248.57
- High: $419.89
- Confidence: High (based on 6 price sources)
```

The initial live sample over-rendered a per-source table. Two prompt fixes
removed unrequested tables and source dumps. The final sample renders the gauge
and source count only.

Invalid Dota app link:

```text
That link is for a Dota 2 inventory item (#570), not CS2 (#730). I can only
value CS2 inventory items.
```

## Gate result

GATE 1 is clean:

- Phase A has independently sourced known-answer fixtures.
- Exact asset attributes are tested exactly.
- Value output is bounded by documented tolerance.
- Private/unreadable behavior declines explicitly.
- LLM routing still delegates all valuation work to deterministic Python.
- Prompt rendering regression found in live testing was fixed, tested,
  rebuilt, and pushed.

Proceed to Phase B inspect-link path research.
