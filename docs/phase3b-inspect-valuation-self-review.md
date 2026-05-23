# Phase 3B inspect-link market-baseline self-review

**Date:** 2026-05-23  
**Gate:** Superseded by corrective review on 2026-05-23
**Decision:** Original "clean" decision was too strong. The inspect route tests
were later corrected to distinguish passthrough tests from live CSGO-API schema
cross-checks.

## Corrective note

The original route test used a fake schema built from expected fixture fields
and mocked price rows derived from the same expected values it asserted on.
That proved response shaping and baseline math, not live schema correctness.
Corrective work renamed those tests as passthrough/shape tests and added
optional live CSGO-API cross-checks under `pytest -m network`.

## Phase B commits reviewed

- `48d19af feat(asset): value modern inspect links`
- `41b2d23 fix(bot): avoid baseline premium speculation`
- `2397899 fix(bot): tighten baseline limitation rendering`
- `9a9dbb0 fix(bot): stop after market baseline`
- `caaabdf fix(bot): make market baseline final`
- `61e7b39 fix(bot): render baseline range as bullets`
- `9c32975 fix(bot): constrain legacy inspect decline`

## Verified inspect-data path

ADR 029 records the current split:

- Modern encoded CS2 inspect links can be decoded offline. Phase B uses
  `cs2-inspect-lite` for local decoding and ByMykel CSGO-API static schema data
  for market hash names and sticker names.
- Legacy `S...A...D<decimal>` / `M...A...D<decimal>` links are only Steam Game
  Coordinator pointers. Resolving them requires Steam account/session state and
  is out of scope.

The deterministic API path is:

1. Decode the modern inspect payload locally.
2. Resolve defindex/paint/wear/quality to `market_hash_name`.
3. Resolve sticker/keychain ids to names.
4. Reuse Phase A's local USD market-baseline computation.
5. Return structured `unreadable` for legacy/invalid links.

The LLM only routes to `market_baseline_inspect_link` and renders the structured
result.

## Fixture evidence

`tests/fixtures/inspect_known_answers.json` uses DMarket fixture rows as
independent evidence. The fixture pins:

| Item | Exact attributes | Value expectation |
|---|---|---:|
| `Souvenir MP9 \| Hot Rod (Factory New)` | asset id `51590003382`, defindex `34`, quality `12`, float `0.035739749670028687`, seed `169`, paint id `33`, four sticker names | `$228.27` within 30% |
| `StatTrak M4A1-S \| Cyrex (Field-Tested)` | asset id `51313391045`, defindex `60`, quality `9`, float `0.20269429683685303`, seed `712`, paint id `360`, two sticker names | `$166.96` within 30% |
| `Desert Eagle \| Oxide Blaze (Factory New)` | asset id `51214828499`, defindex `1`, quality `4`, float `0.06637155264616013`, seed `520`, paint id `645`, zero stickers | `$1.58` within 30% |

The tests first assert the fixture still matches the independent DMarket rows,
then assert the offline decoder and route reproduce those attributes and baseline
tolerances.

## Verification

Final required verification after the last prompt guard:

```text
uv run ruff check .
All checks passed!

uv run pytest
530 passed, 1 deselected, 1 warning in 10.76s
```

After commits, services were rebuilt with:

```text
docker compose up -d --build api bot
docker compose up -d --build bot
```

The API reached healthy state and the bot restarted.

## Live samples

Modern encoded inspect link, through the bot:

```text
Souvenir MP9 | Hot Rod (Factory New)

- Float: 0.03574 (FN)
- Paint Seed: 169
- Stickers:
  - ELEAGUE (Gold) | Boston 2018
  - Cloud9 (Gold) | Boston 2018
  - Skadoodle (Gold) | Boston 2018
  - Virtus.Pro (Gold) | Boston 2018

Market Baseline Range (USD)
- Low: $150.13
- Mid: $249.90
- High: $419.89
- Confidence: High - based on 6 local price points
```

Legacy pointer inspect link, through the bot:

```text
That inspect link is a legacy Steam Game Coordinator pointer - it can't be
resolved directly here without an active Steam session.

To get a market baseline, please paste one of these instead:
- A modern encoded CS2 inspect link
- A public Steam inventory item URL from steamcommunity.com
```

## Gate result

Corrective status:

- Modern inspect links are decoded offline without Steam or CSFloat account
  state.
- Legacy pointer links are declined explicitly under the scope boundary.
- Phase B reuses Phase A's market-baseline computation.
- Known-answer inspect fixtures are research-backed by DMarket rows and can be
  checked against the live CSGO-API schema.
- Full test suite and live API/bot checks passed.
- Commits have been pushed to GitHub per the operator's later instruction.

Remaining limitation: the market baseline is still a market-name USD baseline.
Sticker, charm, float, and pattern premiums are surfaced as attributes but not
independently repriced.
