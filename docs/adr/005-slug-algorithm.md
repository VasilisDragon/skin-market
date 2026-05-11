# ADR 005 — Auto-generated slugs with a fixed glyph map

**Status:** Accepted
**Date:** 2026-05-11

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

## Consequences

- **Pro:** slugs are predictable from the name; you can derive a slug to
  test a URL without querying the DB.
- **Pro:** no human-curation burden as the watchlist grows.
- **Pro:** the glyph-map is the only customization point; new Steam
  glyphs are a one-line addition to `_GLYPH_REPLACEMENTS`.
- **Con:** algorithm version is implicit in the codebase, not in the DB.
  If the slug function changes and we forget the regeneration migration,
  new items get new-style slugs while old items keep old-style — a silent
  inconsistency. Mitigation: an item-create test in v2+ should assert
  `items.slug == slugify(items.market_hash_name)` for at least a sample.
- **Con:** unique-constraint collisions are theoretically possible if two
  distinct `market_hash_name`s produce the same slug. At v1 watchlist
  size we have zero collisions; the constraint will surface any future
  collision loudly as an insert error.
- **Related:** the canonical key is still `market_hash_name`, not `slug`.
  The collector UPSERTs against `market_hash_name`; the bot/API queries
  by `slug`. Both are unique-indexed.
