# ADR 029 - Modern inspect-link valuation

**Status:** Accepted  
**Date:** 2026-05-23  
**Related:** ADR 016 (bot runtime), ADR 027 (cost control), ADR 028
(public-inventory asset valuation)

## Context

Phase B adds valuation for raw CS2 inspect links. The scope boundary forbids
paths that require a CSFloat account, Steam 2FA, or prior CSFloat trade
history. It also forbids half-building account-gated Steam Game Coordinator
paths.

Research found two inspect-link families:

- Legacy `S...A...D<decimal>` or `M...A...D<decimal>` links are pointers. The
  archived `csfloat/inspect` service documents that resolving those pointers
  required Steam accounts with a copy of CS:GO, bot login data, and first-login
  2FA handling. That path is out of scope.
- Modern March 2026 CS2 links self-encode item properties in the inspect link
  payload. `csfloat/inspect` is archived and now directs users to
  `@csfloat/cs2-inspect-serializer`; the Python `cs2-inspect-lite` package
  documents the same split: masked/hybrid links decode offline, classic links
  need the Game Coordinator.

References:

- <https://github.com/csfloat/inspect>
- <https://github.com/csfloat/cs-inspect-serializer>
- <https://github.com/Helyux/cs2inspect>
- <https://pypi.org/project/cs2-inspect-lite/>
- <https://bymykel.com/CSGO-API/>

## Decision

Support modern encoded CS2 inspect links only.

The API route is:

```text
POST /asset-valuations/inspect
```

with body:

```json
{"inspect_url": "steam://run/730//+csgo_econ_action_preview%20..."}
```

The deterministic API path is:

1. Decode the modern inspect payload locally with `cs2-inspect-lite`.
2. Decline classic/legacy pointer links with `status="unreadable"` and
   `reason="legacy_inspect_link"`.
3. Resolve `defindex`, `paintindex`, wear, and quality to
   `market_hash_name` through the public ByMykel CSGO-API static schema.
4. Resolve sticker/keychain ids through the same public schema.
5. Reuse ADR 028's local USD value-gauge computation for the resolved
   market hash name.

The LLM still does not parse links, decode payloads, call external schema
sources, or compute values. It only chooses `value_inspect_link` and renders
the structured result.

## Known-answer fixture

`tests/fixtures/inspect_known_answers.json` uses DMarket fixture rows as
independent evidence. Each row supplies a real modern inspect link plus exact
DMarket attributes and a listing price. Tests assert the fixture still matches
the DMarket row, then assert the offline decoder reproduces the exact asset
attributes and the valuation route stays inside the documented tolerance.

Pinned cases:

| Item | Source evidence | Exact attributes checked | Value expectation |
|---|---:|---|---:|
| `Souvenir MP9 \| Hot Rod (Factory New)` | `tests/fixtures/dmarket/mp9-hot-rod-factory-new.json` object 0 | asset id `51590003382`, defindex `34`, quality `12`, float `0.035739749670028687`, seed `169`, paint id `33`, four sticker names | `$228.27` within 30% |
| `StatTrak M4A1-S \| Cyrex (Field-Tested)` | `tests/fixtures/dmarket/m4a1-s-cyrex-field-tested.json` object 0 | asset id `51313391045`, defindex `60`, quality `9`, float `0.20269429683685303`, seed `712`, paint id `360`, two sticker names | `$166.96` within 30% |
| `Desert Eagle \| Oxide Blaze (Factory New)` | `tests/fixtures/dmarket/desert-eagle-blaze-factory-new.json` object 4 | asset id `51214828499`, defindex `1`, quality `4`, float `0.06637155264616013`, seed `520`, paint id `645`, zero stickers | `$1.58` within 30% |

## Consequences

- Modern inspect links work without Steam credentials or CSFloat account state.
- Legacy pointer links are an explicit unsupported state, not a silent fallback.
- Item and sticker names depend on the public CSGO-API schema being reachable.
  If the schema cannot be loaded, the API still returns decoded exact
  attributes, but no market hash name or value gauge.
- Value computation remains shared with Phase A and retains the same limitation:
  sticker, charm, float, and pattern premiums are surfaced as attributes but are
  not independently repriced.

## Rejected

- **Self-host `csfloat/inspect`.** Rejected. The repository is archived, and
  its install path requires Steam bot login data plus 2FA handling for some
  accounts.
- **Call CSFloat account APIs.** Rejected by the session boundary.
- **Use legacy links by contacting the Steam Game Coordinator.** Rejected by
  the session boundary because that requires Steam account/session state.
- **Duplicate Phase A value math.** Rejected. The implementation reuses the
  shared `build_value_gauge` and local USD price-point loader.
