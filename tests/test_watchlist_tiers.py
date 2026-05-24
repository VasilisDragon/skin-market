"""Tests for api/watchlist_tiers.py — tier helper used by every
item-level API route.

The helper is DB-free (it reads ``data/watchlist.yaml``), so these
tests construct synthetic YAML fixtures in ``tmp_path`` and use
``reload()`` to point the module at them. Tests run without a
DATABASE_URL.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api import watchlist_tiers


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture
def _isolated_tiers(tmp_path: Path):
    """Each test gets a fresh cache pointed at a tmp YAML; reset
    after the test so other suites pick up the real watchlist."""
    yield tmp_path
    watchlist_tiers.reload(watchlist_tiers.DEFAULT_WATCHLIST_PATH)


_BASIC_YAML = """\
schema_version: 3
sources:
  - name: skinport
    base_url: https://api.skinport.com
    rate_limit_per_minute: 8
    enabled: true
    denomination: usd
items:
  - market_hash_name: "Curated Sentinel One (Factory New)"
    item_type: rifle
    weapon_name: "DSO"
    skin_name: "One"
    wear: "Factory New"
    tier: curated
  - market_hash_name: "Curated Sentinel Two (Factory New)"
    item_type: rifle
    weapon_name: "DST"
    skin_name: "Two"
    wear: "Factory New"
    tier: curated
  - market_hash_name: "Featured Sentinel (Factory New)"
    item_type: rifle
    weapon_name: "BS"
    skin_name: "Featured"
    wear: "Factory New"
    tier: featured
"""


class TestGetTier:
    def test_curated_item_returns_curated(self, _isolated_tiers: Path) -> None:
        yaml_path = _isolated_tiers / "watchlist.yaml"
        _write_yaml(yaml_path, _BASIC_YAML)
        watchlist_tiers.reload(yaml_path)
        assert (
            watchlist_tiers.get_tier("Curated Sentinel One (Factory New)")
            == "curated"
        )

    def test_featured_item_returns_featured(self, _isolated_tiers: Path) -> None:
        yaml_path = _isolated_tiers / "watchlist.yaml"
        _write_yaml(yaml_path, _BASIC_YAML)
        watchlist_tiers.reload(yaml_path)
        assert (
            watchlist_tiers.get_tier("Featured Sentinel (Factory New)")
            == "featured"
        )

    def test_unknown_in_yaml_returns_substrate(
        self, _isolated_tiers: Path
    ) -> None:
        """An item NOT in YAML — caller's precondition is "this item
        exists in the items table." That's the substrate case."""
        yaml_path = _isolated_tiers / "watchlist.yaml"
        _write_yaml(yaml_path, _BASIC_YAML)
        watchlist_tiers.reload(yaml_path)
        assert (
            watchlist_tiers.get_tier("Orphaned Item (Factory New)")
            == "substrate"
        )

    def test_empty_market_hash_name_raises(
        self, _isolated_tiers: Path
    ) -> None:
        """Empty string is a programmer error — caller forgot to
        verify the items-table existence check."""
        yaml_path = _isolated_tiers / "watchlist.yaml"
        _write_yaml(yaml_path, _BASIC_YAML)
        watchlist_tiers.reload(yaml_path)
        with pytest.raises(ValueError, match="non-empty market_hash_name"):
            watchlist_tiers.get_tier("")


class TestReload:
    def test_reload_picks_up_yaml_changes(
        self, _isolated_tiers: Path
    ) -> None:
        yaml_path = _isolated_tiers / "watchlist.yaml"
        _write_yaml(yaml_path, _BASIC_YAML)
        watchlist_tiers.reload(yaml_path)
        assert (
            watchlist_tiers.get_tier("Featured Sentinel (Factory New)")
            == "featured"
        )

        # Same item flipped to deep.
        updated = _BASIC_YAML.replace(
            'market_hash_name: "Featured Sentinel (Factory New)"\n'
            '    item_type: rifle\n'
            '    weapon_name: "BS"\n'
            '    skin_name: "Featured"\n'
            '    wear: "Factory New"\n'
            '    tier: featured',
            'market_hash_name: "Featured Sentinel (Factory New)"\n'
            '    item_type: rifle\n'
            '    weapon_name: "BS"\n'
            '    skin_name: "Featured"\n'
            '    wear: "Factory New"\n'
            '    tier: curated',
        )
        _write_yaml(yaml_path, updated)
        # Without reload, cache still says "broad".
        assert (
            watchlist_tiers.get_tier("Featured Sentinel (Factory New)")
            == "featured"
        )
        watchlist_tiers.reload(yaml_path)
        assert (
            watchlist_tiers.get_tier("Featured Sentinel (Factory New)")
            == "curated"
        )

    def test_reload_with_invalid_yaml_raises(
        self, _isolated_tiers: Path
    ) -> None:
        """A YAML missing required ``tier:`` on an item must fail-fast
        at load time, exactly the way seed_watchlist.load_watchlist
        fails fast. Tier helper delegates validation to load_watchlist."""
        bad_yaml = """\
        schema_version: 3
        sources:
          - name: skinport
            base_url: https://api.skinport.com
            rate_limit_per_minute: 8
            enabled: true
            denomination: usd
        items:
          - market_hash_name: "Missing Tier Item (Factory New)"
            item_type: rifle
        """
        yaml_path = _isolated_tiers / "watchlist.yaml"
        _write_yaml(yaml_path, bad_yaml)
        watchlist_tiers.reload(yaml_path)
        with pytest.raises(ValueError, match="tier"):
            watchlist_tiers.get_tier("Missing Tier Item (Factory New)")


class TestLiveWatchlistLoads:
    """Smoke test: the real data/watchlist.yaml loads via the helper.
    Catches accidents where the YAML schema_version or per-item shape
    drifts away from what load_watchlist accepts (which would break
    every API route at startup)."""

    def test_default_yaml_loads(self) -> None:
        watchlist_tiers.reload(watchlist_tiers.DEFAULT_WATCHLIST_PATH)
        # First item we know is in the watchlist.
        tier = watchlist_tiers.get_tier(
            "AK-47 | Redline (Field-Tested)"
        )
        assert tier in {"curated", "featured"}, (
            f"Real watchlist item returned tier={tier!r}; expected "
            f"curated or featured"
        )
