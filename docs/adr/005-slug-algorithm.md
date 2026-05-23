# ADR 005 — Auto-generated slugs with transliteration

**Status:** Accepted
**Date:** 2026-05-11
**Amended:** 2026-05-23 — slug v2

## Context

Each row in `items` needs a stable, URL-safe handle that the FastAPI routes
and Discord bot can use for lookups. Three things have to be true of this
handle:

1. **Deterministic.** Re-deriving from the same `market_hash_name` always
   produces the same output. Two collectors writing the same item must
   agree on the slug.
2. **URL-safe.** It will appear in paths like `/items/<slug>/price`. RFC
   3986 unreserved characters only: `[a-z0-9-]`.
3. **Readable.** A human eyeballing logs should recognize the item.
   `awp-asiimov-field-tested` is much better than a UUID or hash.

The catch: Steam's `market_hash_name` strings contain CS2-specific glyphs
that don't decompose cleanly:

- `★` (U+2605 BLACK STAR) — prefixes every knife and glove
- `™` (U+2122 TRADE MARK SIGN) — embedded in `StatTrak™`
- `®`, `©` — defensive; appear in some community capsule names

Plus the usual punctuation: `|`, `(`, `)`, `'`, `-`.

## Decision

Slugs are **auto-generated** from `market_hash_name` at insert time, using
the algorithm in `db/naming.slugify`. They are stored in `items.slug`,
unique-constrained, and never recomputed implicitly.

The algorithm is **versioned v1**. If output changes for any input that has
an existing row in the DB, we ship a regeneration migration that touches
every affected slug deliberately.

### Algorithm (v1)

```
1. NFC-normalize the input
2. Replace known CS2 glyphs:
     ™, ®, ©  -> "" (drop; adjacent words are already unique)
     ★        -> " star " (becomes the token "star")
3. Lowercase
4. Map ASCII punctuation [|()/,.:;!?[]{}'] to spaces
5. Drop anything not in [a-z0-9-\s] (sweeps remaining unicode)
6. Collapse whitespace -> single hyphens
7. Collapse hyphen runs
8. Strip leading/trailing hyphens
```

### Algorithm (v2 — 2026-05-23)

Slug v2 keeps the v1 output for existing ASCII-safe skin names, but stops
dropping meaningful non-ASCII characters. It was introduced after the Phase 2c
catalog bootstrap surfaced a real collision:

- `Desert Eagle | Sunset Storm 壱 (Factory New)`
- `Desert Eagle | Sunset Storm 弐 (Factory New)`

Under v1 both produced `desert-eagle-sunset-storm-factory-new`; under v2 they
produce:

- `desert-eagle-sunset-storm-yi-factory-new`
- `desert-eagle-sunset-storm-er-factory-new`

The v2 algorithm:

```
1. NFC-normalize the input
2. Replace known CS2 glyphs:
     ™, ®, ©  -> "" (drop; adjacent words are already unique)
     ★        -> " star " (becomes the token "star")
3. Replace "/" with " slash " so names such as Holo/Foil and Holo-Foil
   do not collapse to the same slug
4. Transliterate remaining non-ASCII characters with Unidecode
5. For any non-ASCII character with no useful transliteration, emit a
   deterministic codepoint token: u<hex>
6. Lowercase
7. Map ASCII punctuation [|(),.:;!?[]{}'] to spaces
8. Drop anything not in [a-z0-9-\s]
9. Collapse whitespace -> single hyphens
10. Collapse hyphen runs
11. Strip leading/trailing hyphens
```

The migration discipline for v2:

- `db.naming.SLUG_ALGORITHM_VERSION = 2`
- Alembic migration `0012_slug_algorithm_v2.py` carries a copy of the v2
  algorithm and regenerates all existing `items.slug` values.
- `scripts/verify_slug_uniqueness.py` checks current DB items plus the live
  top-5,000 ranked Pricempire catalog names before commit.
- Regression tests pin ASCII stability, the Sunset Storm pair, slash-vs-hyphen
  disambiguation, and parity between the runtime function and the migration
  copy.

## Alternatives considered

- **Manually curated slugs**: every item gets a hand-written slug at seed
  time. Rejected. The watchlist grows; humans make typos; collisions go
  undetected until a query returns the wrong row. A failure mode for
  zero benefit.
- **Hash-based slugs** (SHA1 of name, truncated): deterministic and
  URL-safe, but unreadable. Logs become unparseable. Rejected.
- **NFKC normalization** (instead of NFC): would convert `™` to the
  ASCII letters `TM`, which then concatenate with `StatTrak` to produce
  `stattraktm-...`. Rejected — we want `™` to disappear, not transliterate.
- **Library: `python-slugify`, `awesome-slugify`**: both work, but neither
  knows about `★` as a meaningful token, so we'd have to pre-process the
  input before handing it to them. At that point, the fifteen lines of
  `slugify()` in this repo are simpler than a library dependency plus
  pre-processing.
- **Library: `Unidecode`**: accepted for v2. It solves the exact
  non-ASCII-transliteration problem while allowing this repo to retain its
  CS2-specific pre-processing rules for `★`, `™`, `®`, and `©`.

## Consequences

- **Pro:** slugs are predictable from the name; you can derive a slug to
  test a URL without querying the DB.
- **Pro:** no human-curation burden as the watchlist grows.
- **Pro:** the glyph-map is the only customization point; new Steam
  glyphs are a one-line addition to `_GLYPH_REPLACEMENTS`.
- **Con:** algorithm version is now explicit in the codebase, not in the DB.
  If the slug function changes again and we forget the regeneration migration,
  new items get new-style slugs while old items keep old-style — a silent
  inconsistency. Mitigation: slug-vN changes must bump
  `SLUG_ALGORITHM_VERSION`, ship a regeneration migration, and keep the
  migration parity test.
- **Con:** unique-constraint collisions remain theoretically possible if two
  distinct `market_hash_name`s produce the same slug. Slug v2 closes the
  observed Sunset Storm and slash-vs-hyphen collision classes. The
  pre-commit verifier is the operational guardrail for the current DB plus
  rank-driven catalog seed surface.
- **Related:** the canonical key is still `market_hash_name`, not `slug`.
  The collector UPSERTs against `market_hash_name`; the bot/API queries
  by `slug`. Both are unique-indexed.
