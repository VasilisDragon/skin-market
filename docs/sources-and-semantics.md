# Sources and Semantics

The skin-market collects prices from multiple CS2 marketplaces with
**materially different denomination and listing semantics**. The product
intentionally does NOT collapse these into a single "true price" — it
surfaces each source's price with its own meaning, and looks for signal
in their disagreement.

This document is the canonical reference for what each source means.
Analytics, the bot's reply formatting, and any future contributor must
treat the source set as dynamic (iterate `sources WHERE enabled`, never
hardcode names) and respect the per-source semantics below.

## Steam Community Market (`source.name = 'steam_market'`)

- **Denomination: Steam Wallet credit**, not USD. The number Steam
  returns from `priceoverview` (`$X.YY` in our locale-locked query) is
  a wallet-credit amount that *looks* like dollars but isn't liquid.
- Steam Wallet credit can be used to buy Steam games and other
  inventory items but **cannot be withdrawn to real money**. Steam's
  EULA is explicit about this.
- This produces a **structural premium of ~30–50%** for normal items:
  someone with wallet credit will pay above-USD to use it rather than
  leave it stranded. The premium is the wallet's illiquidity priced
  into every listing.
- Steam's `lowest_price` is the lowest currently listed price (same
  semantics as Skinport's `min_price`). `median_price` is the median
  of recent sales. `volume` is **24-hour sales count**, a flow
  measurement, not stock.
- "`success: true` with no price fields" is the normal response for
  items with zero current listings — it does NOT mean the item
  doesn't exist or the parser is broken. It means *no one is
  selling this right now*. For ultra-rare items (Howl, Dragon Lore,
  high-tier gloves) this is the steady state most of the time. See
  ADR 006 §3 and ADR 010 (when written) for analytics treatment;
  Phase 4 collector code logs these as INFO and counts them as
  `unavailable` in the cycle summary, alongside genuine parse
  failures. Phase 5 distinguishes them — a long unavailability run is
  *real rarity signal*, not noise to filter.
- Endpoint: `GET https://steamcommunity.com/market/priceoverview/?country=US&currency=1&appid=730&market_hash_name=…`
- Quirks: Steam blocks the default Python User-Agent almost
  immediately (real Chrome UA bypasses it indefinitely); rate-limit
  enforcement is opaque and may require a session cookie if it
  starts 429-ing. See ADR 006 §4 for the cookie escalation plan.

## Skinport (`source.name = 'skinport'`)

- **Denomination: USD**, real money. Skinport pays out to bank/PayPal,
  so its prices reflect the actual cash someone needs to receive to
  hand over the item.
- `min_price` is the lowest currently listed price across **all
  variants of the same `market_hash_name`**. This matters enormously
  for items with variant phases:
  - **Doppler knives** have Phases 1–4, Ruby, Sapphire, and Black
    Pearl, all sharing the same `market_hash_name`. Phase 1 is
    cheap (~$300 for a Karambit); Ruby/Sapphire/Black Pearl can be
    $5,000–$20,000+. When the cheapest Phase 1 sells, `min_price`
    jumps to the next-cheapest listing, which might be a different
    phase entirely. Apparent "4× price spikes in five minutes" on
    Doppler items are this dynamic, NOT data quality issues.
  - **Marble Fade** has Fire-and-Ice, Tricolor, and "patterns"
    similarly causing wide `min_price` ranges for one `market_hash_name`.
  - **Crimson Web** has rare "tier 1 webs" worth multiples of
    standard listings.
- `quantity` is the **count of currently listed items**, a stock
  measurement. Compare to Steam's `volume` (24h sales) at your peril
  — they are different signals. Analytics must filter by source when
  comparing these columns. ADR 008 §2 documents this.
- Endpoint: `GET https://api.skinport.com/v1/items?app_id=730&currency=USD`.
  Returns ~6000 items in one bulk response. Requires
  `Accept-Encoding: br` (brotli) — Skinport returns 406 without it.
  Confirmed empirically; gzip alone is not sufficient.
- No equivalent of Steam's "no listings" oddity: when `quantity = 0`,
  `min_price` is `null`. Collector treats this the same way as
  Steam's `success:false`.

## Why we don't average / pick one as "right"

Given two sources at the same instant:

- **AK Redline FT** might show `$42.92` on Steam and `$28.00` on
  Skinport. That's not a discrepancy — both are correct. The right
  answer to "what's this worth" is **"$28 USD on Skinport, or $42.92
  in Steam wallet credit"**, with the user choosing based on what
  currency they're holding.
- **Karambit Doppler FN** might show `$2,121` on Steam and `$8,143`
  on Skinport. Again, both correct. Skinport's number reflects the
  lowest currently-listed phase, which might be a Ruby; Steam's
  number reflects the cheapest Phase 1 someone is willing to take
  wallet credit for. Different items, same `market_hash_name`.

Averaging these together produces a nonsense number. Picking one as
canonical hides information from the user. The bot's job is to surface
both with their semantic context, and let the human (or downstream
analytics) interpret.

## Anomaly detection: spread, not absolute moves

Phase 5 anomaly detection does NOT auto-flag "implausible" single-source
moves. A Doppler price 4×ing in five minutes IS real information —
filtering it would erase the signal.

What anomaly detection DOES flag:

- **Cross-source divergence**: when Skinport moves 5% but Steam stays
  flat (or vice versa), the spread between sources has changed. That
  is a different signal from "both moved 5% together" (uniform market
  move). The former might indicate variant-mix shifts on Skinport,
  Steam wallet-credit liquidity changes, or arbitrage opportunities;
  the latter is general market drift.
- **Volume anomalies** (Steam only — Skinport's `quantity` is stock,
  not flow): unusual 24h sales counts vs. rolling baseline.

A single-source price spike with no cross-source divergence is logged
but not flagged as an anomaly. A cross-source spread change is flagged
even if absolute prices didn't move much.

ADR 010 (Phase 5) carries the implementation details.

## Triangulation: why three sources beats two

With **two sources**, when they disagree, we can detect it but cannot
resolve which is "more right." The wallet-credit vs USD split makes one
source's price systematically not-comparable to the other in absolute
terms; only the *relative* movement is comparable.

With **three or more sources**, disagreements between any two can be
checked against the third. A Steam outlier looks like a Steam outlier
because the other two agree. A Skinport variant-shift becomes visible
when Steam and the third source both stay flat.

CSFloat (v2 roadmap entry) is the planned third source. It is:

- USD-denominated, real-money payout (similar semantics to Skinport)
- US-leaning operator (Skinport is EU-based)
- An **official API** (no scraping, no Brotli quirks, no UA rotation
  needed)
- The only source with per-listing float-tier data, which v2 needs
  for accurate pricing of items where float matters (Glock Fade,
  StatTrak with low floats, knives with low floats)

The primary reason CSFloat is on the roadmap is float-tier pricing,
but its triangulation value should not be undersold: once it lands,
the analytics can flag "Skinport disagrees with both Steam-equivalent
and CSFloat-equivalent prices" as a meaningful per-source signal,
instead of "Skinport and Steam disagree, which is normal due to
denomination."

Beyond CSFloat, sources to consider for v3+:

- **Buff163** (Chinese market, RMB-denominated, scraping required,
  large market for high-tier items but very different price levels)
- **DMarket** (USD, real-money, official API, growing US presence)
- **BitSkins / Tradeit** (USD or wallet, mixed payout terms)

Adding any of these is a config change — insert a row in `sources`,
fill in `denomination`, write a collector module. The analytics
already iterates `sources WHERE enabled`; no rewrite needed.

## What this means for the code

- **`sources` table** carries `denomination` (`'usd'`,
  `'wallet_credit'`, future `'rmb'`, …). Added in the Phase 5
  migration. The bot/narrative formatters use this to render prices
  honestly ("$42.92 in Steam wallet credit" not "$42.92 USD").
- **Analytics SQL** queries iterate `SELECT id, name FROM sources
  WHERE enabled`. Never hardcode `'steam_market'` or `'skinport'`.
- **Cross-source insights** (view + spread) are computed and stored
  as their own insight types. The view is the per-source breakdown
  needed for the bot's reply; the spread is a per-pair time-series
  for divergence detection.
- **Anomaly detection** flags divergence, not single-source moves.
  A Doppler spike on Skinport with Steam flat is a *cross-source*
  anomaly; the same spike with Steam also spiking is not.
- **Documentation** of source semantics lives here, not scattered
  across code comments. When in doubt, this file is canonical.
