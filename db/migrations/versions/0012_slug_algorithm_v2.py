"""slug algorithm v2 regeneration

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-23

Regenerates stored ``items.slug`` values with slug algorithm v2. The v2
algorithm preserves v1 output for ASCII-safe item names and transliterates
non-ASCII characters so distinct market_hash_names such as Sunset Storm
壱/弐 no longer collapse to the same slug.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from unidecode import unidecode

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GLYPH_REPLACEMENTS: dict[str, str] = {
    "™": "",
    "®": "",
    "©": "",
    "★": " star ",
}
_PUNCT_TO_SPACE = re.compile(r"[|(),.:;!?\[\]{}']")
_NON_SLUG_CHAR = re.compile(r"[^a-z0-9\-\s]")
_WHITESPACE = re.compile(r"\s+")
_HYPHEN_RUN = re.compile(r"-+")


def _normalize_name(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _transliterate_non_ascii(s: str) -> str:
    parts: list[str] = []
    for char in s:
        if char.isascii():
            parts.append(char)
            continue
        transliterated = unidecode(char)
        if any(c.isascii() and c.isalnum() for c in transliterated):
            parts.append(transliterated)
        else:
            parts.append(f" u{ord(char):x} ")
    return "".join(parts)


def _slugify_v2(name: str) -> str:
    s = _normalize_name(name)
    for glyph, replacement in _GLYPH_REPLACEMENTS.items():
        s = s.replace(glyph, replacement)
    s = s.replace("/", " slash ")
    s = _transliterate_non_ascii(s)
    s = s.lower()
    s = _PUNCT_TO_SPACE.sub(" ", s)
    s = _NON_SLUG_CHAR.sub("", s)
    s = _WHITESPACE.sub("-", s.strip())
    s = _HYPHEN_RUN.sub("-", s)
    return s.strip("-")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, market_hash_name, slug FROM items ORDER BY id")
    ).mappings()
    updates: list[dict[str, object]] = []
    names_by_slug: dict[str, list[str]] = {}

    for row in rows:
        new_slug = _slugify_v2(row["market_hash_name"])
        names_by_slug.setdefault(new_slug, []).append(row["market_hash_name"])
        if new_slug != row["slug"]:
            updates.append({"id": row["id"], "slug": new_slug})

    collisions = {
        slug: names
        for slug, names in names_by_slug.items()
        if len(set(names)) > 1
    }
    if collisions:
        sample = "; ".join(
            f"{slug}: {', '.join(names[:3])}"
            for slug, names in list(collisions.items())[:5]
        )
        raise RuntimeError(f"slug v2 collision(s) detected: {sample}")

    if updates:
        bind.execute(
            sa.text("UPDATE items SET slug = :slug WHERE id = :id"),
            updates,
        )


def downgrade() -> None:
    # Forward-only data repair. Regenerating v1 slugs after v2 rows have been
    # inserted can recreate known uniqueness collisions, so downgrade leaves
    # slug values unchanged. Downgrading to base still drops the table later in
    # the migration chain.
    pass
