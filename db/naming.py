"""Item-name utilities: NFC normalization and slug generation.

The slug algorithm is versioned: this is **v1**. If you change behavior in a
way that produces different output for inputs that previously had slugs in
the DB, bump the version and ship a one-shot regeneration migration. Existing
``items.slug`` rows are never recomputed implicitly.
"""

from __future__ import annotations

import re
import unicodedata

# CS2-specific glyphs that appear in Steam market_hash_names. The trademark
# and registered marks drop entirely (the adjacent word — "StatTrak",
# "Karambit", etc. — is already unique enough). The black star prefixes
# every knife and glove name, so it becomes the explicit token "star".
_GLYPH_REPLACEMENTS: dict[str, str] = {
    "™": "",        # ™ TRADE MARK SIGN
    "®": "",        # ® REGISTERED SIGN
    "©": "",        # © COPYRIGHT SIGN
    "★": " star ",  # ★ BLACK STAR
}

_PUNCT_TO_SPACE = re.compile(r"[|()/,.:;!?\[\]{}']")
_NON_SLUG_CHAR = re.compile(r"[^a-z0-9\-\s]")
_WHITESPACE = re.compile(r"\s+")
_HYPHEN_RUN = re.compile(r"-+")


def normalize_name(name: str) -> str:
    """NFC-normalize a market_hash_name.

    Steam already returns NFC, so this is identity on real Steam output. The
    purpose is defending against decomposed-form inputs in test fixtures,
    copy-paste from other tools, or hand-typed strings.
    """
    return unicodedata.normalize("NFC", name)


def slugify(name: str) -> str:
    """Convert a CS2 market_hash_name into a URL-safe slug.

    The output uses only [a-z0-9-] with no leading/trailing hyphens and no
    runs of hyphens. Slugs are stable: re-running on the same input always
    produces the same output, and the output is suitable as a unique key in
    the ``items.slug`` column.

    Examples:
        >>> slugify("AK-47 | Redline (Field-Tested)")
        'ak-47-redline-field-tested'
        >>> slugify("\\u2605 Karambit | Doppler (Factory New)")
        'star-karambit-doppler-factory-new'
        >>> slugify("StatTrak\\u2122 AK-47 | Redline (Field-Tested)")
        'stattrak-ak-47-redline-field-tested'
    """
    s = normalize_name(name)
    for glyph, replacement in _GLYPH_REPLACEMENTS.items():
        s = s.replace(glyph, replacement)
    s = s.lower()
    s = _PUNCT_TO_SPACE.sub(" ", s)
    s = _NON_SLUG_CHAR.sub("", s)
    s = _WHITESPACE.sub("-", s.strip())
    s = _HYPHEN_RUN.sub("-", s)
    return s.strip("-")
