"""Unit tests for db.naming — slug generation and NFC normalization."""

from __future__ import annotations

import pytest

from db.naming import normalize_name, slugify


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("AK-47 | Redline (Field-Tested)", "ak-47-redline-field-tested"),
            ("M4A4 | Howl (Factory New)", "m4a4-howl-factory-new"),
            ("AWP | Dragon Lore (Field-Tested)", "awp-dragon-lore-field-tested"),
            ("USP-S | Kill Confirmed (Field-Tested)", "usp-s-kill-confirmed-field-tested"),
            # Star prefix (knives/gloves)
            ("★ Karambit | Doppler (Factory New)", "star-karambit-doppler-factory-new"),
            (
                "★ M9 Bayonet | Crimson Web (Field-Tested)",
                "star-m9-bayonet-crimson-web-field-tested",
            ),
            # StatTrak (trademark glyph dropped)
            (
                "StatTrak™ AK-47 | Redline (Field-Tested)",
                "stattrak-ak-47-redline-field-tested",
            ),
            # Combined star + StatTrak
            (
                "★ StatTrak™ Karambit | Doppler (Factory New)",
                "star-stattrak-karambit-doppler-factory-new",
            ),
            # Souvenir prefix (ASCII; nothing special)
            (
                "Souvenir AWP | Dragon Lore (Battle-Scarred)",
                "souvenir-awp-dragon-lore-battle-scarred",
            ),
            # Apostrophes get treated as separators
            ("AWP | Man-o'-war (Field-Tested)", "awp-man-o-war-field-tested"),
            # Leading/trailing whitespace
            ("  AK-47 | Redline  ", "ak-47-redline"),
        ],
    )
    def test_known_inputs(self, name: str, expected: str) -> None:
        assert slugify(name) == expected

    def test_output_is_url_safe(self) -> None:
        names = [
            "AK-47 | Redline (Field-Tested)",
            "★ StatTrak™ Karambit | Doppler (Factory New)",
            "AWP | Man-o'-war (Field-Tested)",
            "Souvenir AWP | Dragon Lore (Battle-Scarred)",
        ]
        for name in names:
            slug = slugify(name)
            assert slug, f"empty slug for {name!r}"
            assert all(
                c.isascii() and (c.isalnum() or c == "-") for c in slug
            ), f"unsafe char in {slug!r}"
            assert not slug.startswith("-")
            assert not slug.endswith("-")
            assert "--" not in slug

    def test_deterministic(self) -> None:
        name = "★ StatTrak™ Karambit | Doppler (Factory New)"
        assert slugify(name) == slugify(name)


class TestNormalizeName:
    def test_nfc_idempotent_on_steam_output(self) -> None:
        # Steam returns NFC; running NFC again should be a no-op.
        name = "StatTrak™ AK-47 | Redline (Field-Tested)"
        assert normalize_name(name) == name
        assert normalize_name(normalize_name(name)) == name

    def test_trademark_codepoint_preserved(self) -> None:
        # The exact codepoint must survive — the Steam API's UPSERT key is
        # this string verbatim.
        name = "StatTrak™ AK-47 | Redline (Field-Tested)"
        normalized = normalize_name(name)
        assert "™" in normalized
        assert normalized.encode("utf-8").startswith(b"StatTrak\xe2\x84\xa2")

    def test_star_codepoint_preserved(self) -> None:
        name = "★ Karambit | Doppler (Factory New)"
        assert "★" in normalize_name(name)
