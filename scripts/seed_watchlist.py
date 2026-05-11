"""Seed the watchlist with ~50 popular CS2 items and the two v1 source rows.

Idempotent: re-running is a no-op for rows that already exist (ON CONFLICT
DO NOTHING on the unique constraints).

Usage:
    uv run python -m scripts.seed_watchlist
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.naming import normalize_name, slugify


@dataclass(frozen=True)
class SeedItem:
    market_hash_name: str
    item_type: str
    weapon_name: str
    skin_name: str
    wear: str
    is_stattrak: bool = False
    is_souvenir: bool = False


# Roughly 50 items split across the categories the bot will get asked about.
# market_hash_name strings use the exact form Steam returns (★ = U+2605,
# ™ = U+2122) so the Steam collector can fetch them without translation.
ITEMS: list[SeedItem] = [
    # --- Rifles (15) ---
    SeedItem("AK-47 | Redline (Field-Tested)", "rifle", "AK-47", "Redline", "Field-Tested"),
    SeedItem("AK-47 | Redline (Minimal Wear)", "rifle", "AK-47", "Redline", "Minimal Wear"),
    SeedItem("AK-47 | Asiimov (Battle-Scarred)", "rifle", "AK-47", "Asiimov", "Battle-Scarred"),
    SeedItem("AK-47 | Vulcan (Factory New)", "rifle", "AK-47", "Vulcan", "Factory New"),
    SeedItem("AK-47 | Fire Serpent (Field-Tested)", "rifle", "AK-47", "Fire Serpent", "Field-Tested"),
    SeedItem("M4A1-S | Hyper Beast (Field-Tested)", "rifle", "M4A1-S", "Hyper Beast", "Field-Tested"),
    SeedItem("M4A1-S | Cyrex (Field-Tested)", "rifle", "M4A1-S", "Cyrex", "Field-Tested"),
    SeedItem("M4A1-S | Decimator (Field-Tested)", "rifle", "M4A1-S", "Decimator", "Field-Tested"),
    SeedItem("M4A4 | Howl (Factory New)", "rifle", "M4A4", "Howl", "Factory New"),
    SeedItem("M4A4 | Asiimov (Field-Tested)", "rifle", "M4A4", "Asiimov", "Field-Tested"),
    SeedItem("M4A4 | Neo-Noir (Factory New)", "rifle", "M4A4", "Neo-Noir", "Factory New"),
    SeedItem("AUG | Akihabara Accept (Factory New)", "rifle", "AUG", "Akihabara Accept", "Factory New"),
    SeedItem("FAMAS | Roll Cage (Minimal Wear)", "rifle", "FAMAS", "Roll Cage", "Minimal Wear"),
    SeedItem("Galil AR | Eco (Field-Tested)", "rifle", "Galil AR", "Eco", "Field-Tested"),
    SeedItem("SG 553 | Pulse (Factory New)", "rifle", "SG 553", "Pulse", "Factory New"),
    # --- AWPs (10) ---
    SeedItem("AWP | Asiimov (Field-Tested)", "awp", "AWP", "Asiimov", "Field-Tested"),
    SeedItem("AWP | Hyper Beast (Field-Tested)", "awp", "AWP", "Hyper Beast", "Field-Tested"),
    SeedItem("AWP | Dragon Lore (Field-Tested)", "awp", "AWP", "Dragon Lore", "Field-Tested"),
    SeedItem("AWP | Lightning Strike (Factory New)", "awp", "AWP", "Lightning Strike", "Factory New"),
    SeedItem("AWP | Wildfire (Factory New)", "awp", "AWP", "Wildfire", "Factory New"),
    SeedItem("AWP | Neo-Noir (Field-Tested)", "awp", "AWP", "Neo-Noir", "Field-Tested"),
    SeedItem("AWP | Containment Breach (Field-Tested)", "awp", "AWP", "Containment Breach", "Field-Tested"),
    SeedItem("AWP | Atheris (Minimal Wear)", "awp", "AWP", "Atheris", "Minimal Wear"),
    SeedItem("AWP | Redline (Field-Tested)", "awp", "AWP", "Redline", "Field-Tested"),
    SeedItem("AWP | Man-o'-war (Field-Tested)", "awp", "AWP", "Man-o'-war", "Field-Tested"),
    # --- Pistols (6) ---
    SeedItem("Glock-18 | Fade (Factory New)", "pistol", "Glock-18", "Fade", "Factory New"),
    SeedItem("Glock-18 | Water Elemental (Factory New)", "pistol", "Glock-18", "Water Elemental", "Factory New"),
    SeedItem("Desert Eagle | Blaze (Factory New)", "pistol", "Desert Eagle", "Blaze", "Factory New"),
    SeedItem("Desert Eagle | Printstream (Factory New)", "pistol", "Desert Eagle", "Printstream", "Factory New"),
    SeedItem("USP-S | Kill Confirmed (Field-Tested)", "pistol", "USP-S", "Kill Confirmed", "Field-Tested"),
    SeedItem("USP-S | Neo-Noir (Factory New)", "pistol", "USP-S", "Neo-Noir", "Factory New"),
    # --- Knives (10, all "★" prefixed) ---
    SeedItem("★ Karambit | Doppler (Factory New)", "knife", "Karambit", "Doppler", "Factory New"),
    SeedItem("★ Karambit | Fade (Factory New)", "knife", "Karambit", "Fade", "Factory New"),
    SeedItem("★ Karambit | Tiger Tooth (Factory New)", "knife", "Karambit", "Tiger Tooth", "Factory New"),
    SeedItem("★ M9 Bayonet | Doppler (Factory New)", "knife", "M9 Bayonet", "Doppler", "Factory New"),
    SeedItem("★ M9 Bayonet | Crimson Web (Field-Tested)", "knife", "M9 Bayonet", "Crimson Web", "Field-Tested"),
    SeedItem("★ Butterfly Knife | Tiger Tooth (Factory New)", "knife", "Butterfly Knife", "Tiger Tooth", "Factory New"),
    SeedItem("★ Butterfly Knife | Fade (Factory New)", "knife", "Butterfly Knife", "Fade", "Factory New"),
    SeedItem("★ Bayonet | Marble Fade (Factory New)", "knife", "Bayonet", "Marble Fade", "Factory New"),
    SeedItem("★ Flip Knife | Doppler (Factory New)", "knife", "Flip Knife", "Doppler", "Factory New"),
    SeedItem("★ Huntsman Knife | Fade (Factory New)", "knife", "Huntsman Knife", "Fade", "Factory New"),
    # --- Gloves (5, all "★" prefixed) ---
    SeedItem("★ Sport Gloves | Pandora's Box (Field-Tested)", "glove", "Sport Gloves", "Pandora's Box", "Field-Tested"),
    SeedItem("★ Sport Gloves | Vice (Field-Tested)", "glove", "Sport Gloves", "Vice", "Field-Tested"),
    SeedItem("★ Specialist Gloves | Crimson Kimono (Field-Tested)", "glove", "Specialist Gloves", "Crimson Kimono", "Field-Tested"),
    SeedItem("★ Driver Gloves | King Snake (Field-Tested)", "glove", "Driver Gloves", "King Snake", "Field-Tested"),
    SeedItem("★ Hand Wraps | Cobalt Skulls (Field-Tested)", "glove", "Hand Wraps", "Cobalt Skulls", "Field-Tested"),
    # --- StatTrak (2, exercise the ™ glyph) ---
    SeedItem("StatTrak™ AK-47 | Redline (Field-Tested)", "rifle", "AK-47", "Redline", "Field-Tested", is_stattrak=True),
    SeedItem("StatTrak™ AWP | Asiimov (Field-Tested)", "awp", "AWP", "Asiimov", "Field-Tested", is_stattrak=True),
    # --- Souvenir (1) ---
    SeedItem("Souvenir AWP | Dragon Lore (Battle-Scarred)", "awp", "AWP", "Dragon Lore", "Battle-Scarred", is_souvenir=True),
    # --- SMG (2) ---
    SeedItem("P90 | Death by Kitty (Field-Tested)", "smg", "P90", "Death by Kitty", "Field-Tested"),
    SeedItem("MP9 | Hot Rod (Factory New)", "smg", "MP9", "Hot Rod", "Factory New"),
]


# The two upstream APIs v1 polls. rate_limit_per_minute is advisory — actual
# enforcement happens in the collector module.
SOURCES: list[dict] = [
    {
        "name": "steam_market",
        "base_url": "https://steamcommunity.com/market/",
        "rate_limit_per_minute": 12,
        "enabled": True,
    },
    {
        "name": "skinport",
        "base_url": "https://api.skinport.com/v1/",
        "rate_limit_per_minute": 60,
        "enabled": True,
    },
]


_INSERT_SOURCE_SQL = text(
    """
    INSERT INTO sources (name, base_url, rate_limit_per_minute, enabled)
    VALUES (:name, :base_url, :rlim, :enabled)
    ON CONFLICT (name) DO NOTHING
    """
)

_INSERT_ITEM_SQL = text(
    """
    INSERT INTO items (
        market_hash_name, display_name, slug, item_type,
        weapon_name, skin_name, wear, is_stattrak, is_souvenir
    )
    VALUES (
        :mhn, :disp, :slug, :it,
        :wpn, :skn, :wear, :stt, :sv
    )
    ON CONFLICT (market_hash_name) DO NOTHING
    """
)


def seed() -> tuple[int, int]:
    """Run the seed. Returns (items_in_db, sources_in_db) after the upsert."""
    engine = get_engine()
    with Session(engine) as session:
        for src in SOURCES:
            session.execute(
                _INSERT_SOURCE_SQL,
                {
                    "name": src["name"],
                    "base_url": src["base_url"],
                    "rlim": src["rate_limit_per_minute"],
                    "enabled": src["enabled"],
                },
            )
        for it in ITEMS:
            mhn = normalize_name(it.market_hash_name)
            session.execute(
                _INSERT_ITEM_SQL,
                {
                    "mhn": mhn,
                    "disp": mhn,  # v1: display_name == market_hash_name
                    "slug": slugify(mhn),
                    "it": it.item_type,
                    "wpn": it.weapon_name,
                    "skn": it.skin_name,
                    "wear": it.wear,
                    "stt": it.is_stattrak,
                    "sv": it.is_souvenir,
                },
            )
        session.commit()

    with engine.connect() as conn:
        items_count = conn.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
        sources_count = conn.execute(text("SELECT COUNT(*) FROM sources")).scalar_one()
    return int(items_count), int(sources_count)


def main() -> int:
    items_count, sources_count = seed()
    print(f"Seed complete: {items_count} items, {sources_count} sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
