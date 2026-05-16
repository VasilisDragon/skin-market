"""Tests for ``scripts/watchlist_edit.py``.

``list`` is exercised purely against tmp_path YAML files (no DB needed).
``add`` and ``remove`` call into the seed and FK-aware delete logic, so
they need a reachable Postgres; they skip with the same pattern as
``test_db_roundtrip.py``.

DB-dependent tests are careful to clean up after themselves: every item
inserted into the real ``items`` table during a test is removed (along
with any synthetic ``prices`` rows) in a finalizer. They use a synthetic
``market_hash_name`` that can't collide with any real CS2 item.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.models import Item, Price, Source
from db.naming import normalize_name
from scripts.watchlist_edit import main as watchlist_edit_main

# A name we'd never actually see on Steam, so the DB-touching tests
# can't conflict with real data.
_TEST_NAME = "__TestItem__ | Sentinel (Field-Tested)"


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        return False


_db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


_BASE_YAML = """\
# Test fixture watchlist
schema_version: 2

sources:
  - name: steam_market
    base_url: https://steamcommunity.com/market/
    rate_limit_per_minute: 12
    enabled: true
  - name: skinport
    base_url: https://api.skinport.com/v1/
    rate_limit_per_minute: 60
    enabled: true

items:
  # --- Rifles ---
  - market_hash_name: "AK-47 | Redline (Field-Tested)"
    item_type: rifle
    weapon_name: "AK-47"
    skin_name: "Redline"
    wear: "Field-Tested"
    tier: deep
  - market_hash_name: "M4A4 | Howl (Factory New)"
    item_type: rifle
    weapon_name: "M4A4"
    skin_name: "Howl"
    wear: "Factory New"
    tier: deep
  # --- Snipers ---
  - market_hash_name: "AWP | Asiimov (Field-Tested)"
    item_type: sniper
    weapon_name: "AWP"
    skin_name: "Asiimov"
    wear: "Field-Tested"
    tier: deep
"""


@pytest.fixture
def tmp_watchlist(tmp_path: Path) -> Path:
    """Write a small fixture YAML and return its path."""
    path = tmp_path / "watchlist.yaml"
    path.write_text(_BASE_YAML)
    return path


@pytest.fixture(autouse=True)
def _preserve_source_enabled_flags():
    """Snapshot ``sources.enabled`` before each test and restore on
    teardown. ``seed_watchlist``'s UPSERT clobbers ``enabled`` from the
    test fixture YAML — which flips operator-managed flags (e.g.
    skinport disabled during rate-limit recovery, ADR 013) as a test
    side effect. Restoring keeps the live DB faithful regardless.
    """
    if not _db_reachable():
        yield
        return
    engine = get_engine()
    with Session(engine) as session:
        snapshot = {
            row.name: row.enabled
            for row in session.execute(
                select(Source.name, Source.enabled)
            ).all()
        }
    yield
    with Session(engine) as session:
        for name, was_enabled in snapshot.items():
            session.execute(
                text(
                    "UPDATE sources SET enabled = :e WHERE name = :n"
                ),
                {"e": was_enabled, "n": name},
            )
        session.commit()


@pytest.fixture
def _cleanup_test_item():
    """Delete the synthetic test item (and any rows it accumulated) from
    the DB after each test. Safe to run even if the test never inserted
    the item — the DELETEs are no-ops in that case.
    """
    yield
    name = normalize_name(_TEST_NAME)
    engine = get_engine()
    with Session(engine) as session:
        item_id = session.execute(
            select(Item.id).where(Item.market_hash_name == name)
        ).scalar_one_or_none()
        if item_id is not None:
            session.execute(
                text("DELETE FROM prices WHERE item_id = :i"),
                {"i": item_id},
            )
            session.execute(
                text("DELETE FROM insights WHERE item_id = :i"),
                {"i": item_id},
            )
            session.execute(
                text("DELETE FROM items WHERE id = :i"),
                {"i": item_id},
            )
            session.commit()


class TestSchemaV2Loader:
    """Phase 2b: scripts.seed_watchlist.load_watchlist enforces
    schema_version == 2 and a ``tier:`` field on every item. Test the
    contract directly so a future YAML drift fails loudly.
    """

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "watchlist.yaml"
        path.write_text(body)
        return path

    def test_accepts_schema_v2_with_tier_deep(self, tmp_path: Path) -> None:
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "AK-47 | Redline (Field-Tested)", '
            'item_type: rifle, weapon_name: "AK-47", skin_name: "Redline", '
            'wear: "Field-Tested", tier: deep }\n',
        )
        data = load_watchlist(path)
        assert data["schema_version"] == 2
        assert data["items"][0]["tier"] == "deep"

    def test_accepts_schema_v2_with_tier_broad(self, tmp_path: Path) -> None:
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: skinport, base_url: https://example, "
            "rate_limit_per_minute: 60, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "Some Broad Item (Factory New)", '
            'item_type: rifle, weapon_name: "X", skin_name: "Y", '
            'wear: "Factory New", tier: broad }\n',
        )
        data = load_watchlist(path)
        assert data["items"][0]["tier"] == "broad"

    def test_rejects_schema_v1(self, tmp_path: Path) -> None:
        """A pre-Phase-2b YAML must fail fast so a stale deploy can't
        silently load with no tier semantics."""
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 1\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "AK-47 | Redline (Field-Tested)", '
            'item_type: rifle, weapon_name: "AK-47", skin_name: "Redline", '
            'wear: "Field-Tested" }\n',
        )
        with pytest.raises(ValueError, match="schema_version"):
            load_watchlist(path)

    def test_rejects_missing_tier(self, tmp_path: Path) -> None:
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "AK-47 | Redline (Field-Tested)", '
            'item_type: rifle, weapon_name: "AK-47", skin_name: "Redline", '
            'wear: "Field-Tested" }\n',
        )
        with pytest.raises(ValueError, match="missing required `tier:`"):
            load_watchlist(path)

    def test_rejects_unknown_tier_value(self, tmp_path: Path) -> None:
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "AK-47 | Redline (Field-Tested)", '
            'item_type: rifle, weapon_name: "AK-47", skin_name: "Redline", '
            'wear: "Field-Tested", tier: archive }\n',
        )
        with pytest.raises(ValueError, match="invalid tier"):
            load_watchlist(path)

    # ── Phase 2b Step 6 (ADR 012 §7) dmarket_alias validation ──

    def test_dmarket_alias_list_accepted(self, tmp_path: Path) -> None:
        """Optional dmarket_alias field as a list of non-empty
        strings loads cleanly."""
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: dmarket, base_url: https://example, "
            "rate_limit_per_minute: 20, enabled: true }\n"
            "items:\n"
            '  - market_hash_name: "Desert Eagle | Blaze (Factory New)"\n'
            "    item_type: pistol\n"
            "    tier: deep\n"
            "    dmarket_alias:\n"
            '      - "Desert Eagle | Blaze (Factory New)"\n'
            '      - "Desert Eagle | Blaze Variant (Factory New)"\n',
        )
        data = load_watchlist(path)
        assert data["items"][0]["dmarket_alias"] == [
            "Desert Eagle | Blaze (Factory New)",
            "Desert Eagle | Blaze Variant (Factory New)",
        ]

    def test_dmarket_alias_must_be_list_not_bare_string(
        self, tmp_path: Path
    ) -> None:
        """Bare-string dmarket_alias rejected; field must always be
        a list (even single-entry lists are written as ``- ...``)."""
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: dmarket, base_url: https://example, "
            "rate_limit_per_minute: 20, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "X (Factory New)", '
            'item_type: pistol, tier: deep, '
            'dmarket_alias: "bare-string-not-allowed" }\n',
        )
        with pytest.raises(ValueError, match="expected a list"):
            load_watchlist(path)

    def test_dmarket_alias_rejects_empty_string_entry(
        self, tmp_path: Path
    ) -> None:
        """Each list entry must be a non-empty string."""
        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: dmarket, base_url: https://example, "
            "rate_limit_per_minute: 20, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "X (Factory New)", '
            'item_type: pistol, tier: deep, '
            'dmarket_alias: ["", "valid"] }\n',
        )
        with pytest.raises(
            ValueError, match="not a non-empty string"
        ):
            load_watchlist(path)

    def test_dmarket_alias_on_broad_tier_warns_not_errors(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Broad-tier items with dmarket_alias log a WARN (dead config)
        but don't fail-fast. DMarket isn't polled for broad tier
        (ADR 024), so the field is dead config there."""
        import logging as _logging

        from scripts.seed_watchlist import load_watchlist

        path = self._write(
            tmp_path,
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: skinport, base_url: https://example, "
            "rate_limit_per_minute: 60, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "Broad Item (Factory New)", '
            'item_type: pistol, tier: broad, '
            'dmarket_alias: ["Broad Item (Factory New)"] }\n',
        )
        with caplog.at_level(
            _logging.WARNING, logger="scripts.seed_watchlist"
        ):
            data = load_watchlist(path)
        # Loads without error.
        assert data["items"][0]["tier"] == "broad"
        # WARN message references "dead config" + ADR 024.
        warns = [r.getMessage() for r in caplog.records]
        assert any(
            "dead config" in m and "ADR 024" in m for m in warns
        ), f"expected dead-config + ADR 024 in WARN; got {warns}"


class TestList:
    def test_lists_all_items(
        self, tmp_watchlist: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = watchlist_edit_main(
            ["--watchlist", str(tmp_watchlist), "list"]
        )
        captured = capsys.readouterr().out
        assert rc == 0
        assert "AK-47 | Redline" in captured
        assert "M4A4 | Howl" in captured
        assert "AWP | Asiimov" in captured
        assert "3 item(s)." in captured

    def test_type_filter(
        self, tmp_watchlist: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = watchlist_edit_main(
            ["--watchlist", str(tmp_watchlist), "list", "--type", "rifle"]
        )
        captured = capsys.readouterr().out
        assert rc == 0
        assert "AK-47" in captured
        assert "M4A4" in captured
        assert "AWP" not in captured
        assert "2 item(s)." in captured

    def test_type_filter_no_matches(
        self, tmp_watchlist: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = watchlist_edit_main(
            ["--watchlist", str(tmp_watchlist), "list", "--type", "knife"]
        )
        captured = capsys.readouterr().out
        assert rc == 0
        assert "No items found for item_type='knife'" in captured


class TestAddYAMLOnly:
    """Tests that don't require the DB (exercise the validation paths)."""

    def test_rejects_unknown_type(
        self, tmp_watchlist: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = watchlist_edit_main(
            [
                "--watchlist",
                str(tmp_watchlist),
                "add",
                "--name",
                _TEST_NAME,
                "--type",
                "BOGUS",
                "--weapon",
                "X",
                "--skin",
                "Y",
                "--wear",
                "Z",
            ]
        )
        captured = capsys.readouterr().err
        assert rc == 2
        assert "must be one of" in captured

    def test_rejects_duplicate(
        self, tmp_watchlist: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Adding an item already in the YAML should fail without touching DB.
        rc = watchlist_edit_main(
            [
                "--watchlist",
                str(tmp_watchlist),
                "add",
                "--name",
                "AK-47 | Redline (Field-Tested)",
                "--type",
                "rifle",
                "--weapon",
                "AK-47",
                "--skin",
                "Redline",
                "--wear",
                "Field-Tested",
            ]
        )
        captured = capsys.readouterr().out
        assert rc == 1
        assert "already in" in captured


@_db_required
class TestAddWithDB:
    def test_adds_to_yaml_and_db(
        self,
        tmp_watchlist: Path,
        capsys: pytest.CaptureFixture[str],
        _cleanup_test_item,
    ) -> None:
        rc = watchlist_edit_main(
            [
                "--watchlist",
                str(tmp_watchlist),
                "add",
                "--name",
                _TEST_NAME,
                "--type",
                "rifle",
                "--weapon",
                "TestWeapon",
                "--skin",
                "Sentinel",
                "--wear",
                "Field-Tested",
            ]
        )
        captured = capsys.readouterr().out
        assert rc == 0
        assert "Added" in captured

        # YAML now contains the item
        assert _TEST_NAME in tmp_watchlist.read_text()

        # DB now contains the item
        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(Item.market_hash_name, Item.item_type).where(
                    Item.market_hash_name == normalize_name(_TEST_NAME)
                )
            ).first()
            assert row is not None
            assert row.item_type == "rifle"


@_db_required
class TestRemoveWithDB:
    def test_remove_when_no_prices_succeeds(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        _cleanup_test_item,
    ) -> None:
        # Set up a watchlist YAML that has only our test item.
        watchlist = tmp_path / "watchlist.yaml"
        watchlist.write_text(
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            f'  - {{ market_hash_name: "{_TEST_NAME}", item_type: rifle, '
            'weapon_name: "Test", skin_name: "Sentinel", '
            'wear: "Field-Tested", tier: deep }\n'
        )
        # Seed it into the DB.
        from scripts.seed_watchlist import seed

        seed(watchlist)

        rc = watchlist_edit_main(
            [
                "--watchlist",
                str(watchlist),
                "remove",
                "--name",
                _TEST_NAME,
            ]
        )
        captured = capsys.readouterr().out
        assert rc == 0
        assert "Removed" in captured

        # Item gone from YAML
        assert _TEST_NAME not in watchlist.read_text()

        # Item gone from DB
        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(Item).where(
                    Item.market_hash_name == normalize_name(_TEST_NAME)
                )
            ).scalar_one_or_none()
            assert row is None

    def test_remove_refuses_when_prices_exist_without_force(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        _cleanup_test_item,
    ) -> None:
        watchlist = tmp_path / "watchlist.yaml"
        watchlist.write_text(
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            f'  - {{ market_hash_name: "{_TEST_NAME}", item_type: rifle, '
            'weapon_name: "Test", skin_name: "Sentinel", '
            'wear: "Field-Tested", tier: deep }\n'
        )
        from scripts.seed_watchlist import seed

        seed(watchlist)

        # Manually insert a price row so remove must refuse.
        engine = get_engine()
        with Session(engine) as session:
            item_id = session.execute(
                select(Item.id).where(
                    Item.market_hash_name == normalize_name(_TEST_NAME)
                )
            ).scalar_one()
            source_id = session.execute(
                select(Source.id).where(Source.name == "steam_market")
            ).scalar_one()
            session.execute(
                pg_insert(Price)
                .values(
                    item_id=item_id,
                    source_id=source_id,
                    timestamp=datetime(2099, 6, 1, tzinfo=UTC),
                    price=Decimal("1.23"),
                    volume=1,
                    currency="USD",
                    raw_response={"test": True},
                )
                .on_conflict_do_nothing(
                    index_elements=["item_id", "source_id", "timestamp"]
                )
            )
            session.commit()

        rc = watchlist_edit_main(
            ["--watchlist", str(watchlist), "remove", "--name", _TEST_NAME]
        )
        captured = capsys.readouterr().err
        assert rc == 1
        assert "Refusing to remove" in captured

        # Item still in YAML
        assert _TEST_NAME in watchlist.read_text()

        # Item still in DB
        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(Item).where(
                    Item.market_hash_name == normalize_name(_TEST_NAME)
                )
            ).scalar_one_or_none()
            assert row is not None

    def test_remove_force_deletes_prices_and_item(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        _cleanup_test_item,
    ) -> None:
        watchlist = tmp_path / "watchlist.yaml"
        watchlist.write_text(
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: steam_market, base_url: https://example, "
            "rate_limit_per_minute: 12, enabled: true }\n"
            "items:\n"
            f'  - {{ market_hash_name: "{_TEST_NAME}", item_type: rifle, '
            'weapon_name: "Test", skin_name: "Sentinel", '
            'wear: "Field-Tested", tier: deep }\n'
        )
        from scripts.seed_watchlist import seed

        seed(watchlist)

        engine = get_engine()
        with Session(engine) as session:
            item_id = session.execute(
                select(Item.id).where(
                    Item.market_hash_name == normalize_name(_TEST_NAME)
                )
            ).scalar_one()
            source_id = session.execute(
                select(Source.id).where(Source.name == "steam_market")
            ).scalar_one()
            session.execute(
                pg_insert(Price)
                .values(
                    item_id=item_id,
                    source_id=source_id,
                    timestamp=datetime(2099, 6, 2, tzinfo=UTC),
                    price=Decimal("9.99"),
                    volume=5,
                    currency="USD",
                    raw_response={"test": True},
                )
                .on_conflict_do_nothing(
                    index_elements=["item_id", "source_id", "timestamp"]
                )
            )
            session.commit()

        rc = watchlist_edit_main(
            [
                "--watchlist",
                str(watchlist),
                "remove",
                "--name",
                _TEST_NAME,
                "--force",
            ]
        )
        captured = capsys.readouterr().out
        assert rc == 0
        assert "Deleted 1 prices rows" in captured

        # Item gone from DB and YAML
        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(Item).where(
                    Item.market_hash_name == normalize_name(_TEST_NAME)
                )
            ).scalar_one_or_none()
            assert row is None
        assert _TEST_NAME not in watchlist.read_text()
