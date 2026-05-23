# ADR 038 - Asset premium evidence layer

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 028, ADR 029, ADR 037

## Context

ADR 037 concluded that a broad per-asset repricer is not yet defensible. The
system can read exact asset attributes and can compute a market-name baseline,
but it lacks approved premium-pricing sources and an operator-confirmed
confirmed-sales corpus.

Users still benefit from knowing why a market-name baseline may be incomplete.
For example, a low-float Doppler gem, a Crimson Web with rank metadata, or a
sticker-heavy Howl should not be rendered as if the generic baseline were the
whole story.

## Decision

Attach a deterministic `evidence` object to the existing asset-baseline
responses:

```text
POST /asset-valuations/inventory
POST /asset-valuations/inspect
POST /asset-valuations/inventory/summary
```

The evidence object contains:

- normalized decoded attributes,
- deterministic premium-driver flags,
- a static signal-availability map,
- an honest summary line.

The evidence layer does not return any premium price, premium range, premium
multiplier, or estimated true value. It does not call CSFloat, CS2BlueGem, BUFF,
or any other new marketplace/source. It only organizes attributes already
available through the existing inventory/inspect paths.

## Evidence Shape

Single-asset responses include:

```json
{
  "evidence": {
    "attributes": {
      "market_hash_name": "...",
      "float_value": "...",
      "wear_band": {
        "code": "factory_new",
        "name": "Factory New",
        "min_float": "0",
        "max_float": "0.07",
        "position_pct": "...",
        "float_position": "low|standard",
        "low_float_threshold_pct": "15"
      },
      "paint_seed": 400,
      "paint_id": 415,
      "is_stattrak": true,
      "is_souvenir": false,
      "ranks": {"low_rank": 3, "high_rank": 14},
      "stickers": [],
      "charms": []
    },
    "driver_flags": [
      {
        "code": "low_float_for_wear_band",
        "label": "low float for wear band",
        "present": true,
        "category": "low",
        "explanation": "...",
        "signal_status": "not_available"
      }
    ],
    "signal_availability": {
      "low_float_for_wear_band": {
        "status": "not_available",
        "source": null,
        "explanation": "..."
      }
    },
    "summary": "..."
  }
}
```

Portfolio summary responses include a portfolio-level `evidence` object with
priced/unpriced counts, driver counts, signal availability for detected
drivers, and a summary. The sampled `top_items`, `largest_spread_items`, and
`unpriced_sample` rows also carry per-item evidence.

## Driver Flags

Driver flags are pure Python comparisons over already-decoded attributes:

- `low_float_for_wear_band`: true when the asset float is in the lowest 15% of
  its wear band.
- `applied_stickers`: true when any applied sticker is present.
- `applied_charms`: true when any applied charm is present.
- `pattern_sensitive_family`: true when the market name matches configured
  family names such as Case Hardened, Heat Treated, Fade, Marble Fade, Doppler,
  Gamma Doppler, or Crimson Web.
- `phase_already_in_market_name`: true when a Doppler/Gamma phase such as Ruby,
  Sapphire, Black Pearl, Emerald, or Phase 1-4 is already separated in the
  market name.
- `rank_present`: true when low/high rank metadata is present.

These flags are not prices. They only explain why the baseline may be
incomplete for a specific asset.

## Signal Availability

The static `PREMIUM_SIGNAL_AVAILABILITY` map starts with the honest current
state:

- float premiums: not available,
- applied-sticker premiums: not available,
- applied-charm premiums: not available,
- pattern-family premiums: not available,
- rank-to-price premiums: not available,
- named Doppler/Gamma phase: covered by the market-name baseline, with no extra
  phase premium computed.

This is the intended future seam. A future source can change one capability
entry from unavailable to available without changing the response shape, but
that requires a separate goal, approved source access, and validation against
operator-supplied confirmed sales.

## Bot Rendering

The bot system prompt now renders evidence after the market baseline under
`Premium Evidence (Not Priced)`. It must state that detected drivers are present
but not priced, and must not invent premium dollar amounts, ranges, multipliers,
buyer-demand commentary, or exact-asset value estimates.

## Consequences

- Users get a more honest answer for high-tier exact assets without receiving a
  fabricated appraisal.
- The deterministic core remains intact: Python computes flags; the LLM only
  renders the returned structure.
- The response shape can support future premium sources without reworking bot
  rendering.
- Tests guard both the deterministic driver logic and the absence of premium
  price fields.

## Rejected

- **Return a rough premium estimate with a disclaimer.** Rejected. The project
  still lacks confirmed sales and an approved premium source.
- **Wire CSFloat or CS2BlueGem now.** Rejected by the session boundary.
- **Scrape community guides/calculators for numeric premiums.** Rejected. Those
  are research context, not a production pricing source.
