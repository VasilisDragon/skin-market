"""Analytics tests.

Exercises moving_averages, cross_source, and anomaly_detection against
a real database with crafted ``prices`` rows. Each test sets up a
"sentinel" item that no real CS2 watchlist contains and cleans it up
after — keeping production data intact when the suite runs.

The narrative job is tested with a mocked Ollama client; the SQL parts
that gather inputs are exercised against the real DB.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from analytics import (
    anomaly_detection,
    cross_source,
    moving_averages,
    narrative,
)
from analytics.ollama_client import OllamaError
from db.connection import get_engine
from db.models import Item, Price, Source


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except (OperationalError, Exception):
        return False


_db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


_SENTINEL_NAME = "__AnalyticsTest__ | Sentinel (Field-Tested)"


@pytest.fixture
def sentinel_item():
    """Insert a sentinel item; clean up its prices + insights + the item
    itself after the test."""
    engine = get_engine()
    item_id = uuid.uuid4()
    with Session(engine) as session:
        # Use an unconditional insert because we control the name space.
        session.execute(
            text(
                """
                INSERT INTO items (
                    id, market_hash_name, display_name, slug, item_type,
                    weapon_name, skin_name, wear
                )
                VALUES (
                    :id, :name, :name, :slug, 'rifle',
                    'TestWeapon', 'Sentinel', 'Field-Tested'
                )
                ON CONFLICT (market_hash_name) DO NOTHING
                """
            ),
            {
                "id": item_id,
                "name": _SENTINEL_NAME,
                "slug": "__analyticstest__-sentinel-field-tested",
            },
        )
        # Read back the ID in case ON CONFLICT skipped the insert (an
        # earlier run left it behind).
        item_id = session.execute(
            select(Item.id).where(Item.market_hash_name == _SENTINEL_NAME)
        ).scalar_one()
        # Clean any leftover prices/insights from prior runs.
        session.execute(
            text("DELETE FROM prices WHERE item_id = :i"), {"i": item_id}
        )
        session.execute(
            text("DELETE FROM insights WHERE item_id = :i"), {"i": item_id}
        )
        session.commit()
        yield item_id

        # Teardown.
        session.execute(
            text("DELETE FROM prices WHERE item_id = :i"), {"i": item_id}
        )
        session.execute(
            text("DELETE FROM insights WHERE item_id = :i"), {"i": item_id}
        )
        session.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
        session.commit()


def _insert_price(session, item_id, source_id, ts, price, volume=10):
    session.execute(
        pg_insert(Price)
        .values(
            item_id=item_id,
            source_id=source_id,
            timestamp=ts,
            price=Decimal(str(price)),
            volume=volume,
            currency="USD",
            raw_response={"synthetic": True},
        )
        .on_conflict_do_nothing(
            index_elements=["item_id", "source_id", "timestamp"]
        )
    )


def _source_id(session, name: str) -> int:
    return session.execute(
        select(Source.id).where(Source.name == name)
    ).scalar_one()


@pytest.fixture
def all_known_sources_enabled():
    """Ensure steam_market, skinport, and dmarket are all enabled for
    the duration of a cross-source test. The live DB may have one or
    more disabled (e.g. skinport during rate-limit recovery — ADR 013);
    analytics filters ``WHERE s.enabled = TRUE`` so a disabled source
    would silently drop its observations from the test's assertions.
    Snapshot and restore on teardown.
    """
    engine = get_engine()
    with Session(engine) as session:
        snapshot = dict(
            session.execute(
                text(
                    "SELECT name, enabled FROM sources WHERE name IN "
                    "('steam_market', 'skinport', 'dmarket')"
                )
            ).all()
        )
        session.execute(
            text(
                "UPDATE sources SET enabled = TRUE "
                "WHERE name IN ('steam_market', 'skinport', 'dmarket')"
            )
        )
        session.commit()
        try:
            yield
        finally:
            for name, was_enabled in snapshot.items():
                session.execute(
                    text(
                        "UPDATE sources SET enabled = :e WHERE name = :n"
                    ),
                    {"e": was_enabled, "n": name},
                )
            session.commit()


@_db_required
class TestMovingAverages:
    def test_writes_one_row_per_window_per_source(
        self, sentinel_item, all_known_sources_enabled
    ):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 7, 1, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            skinport_id = _source_id(session, "skinport")

            # 10 readings over the last 7 days from each source.
            for i in range(10):
                ts = now - timedelta(days=i * 0.5)
                _insert_price(session, item_id, steam_id, ts, 40 + i)
                _insert_price(session, item_id, skinport_id, ts, 25 + i * 0.5)
            session.commit()

            wrote = moving_averages.compute_and_store(session, now=now)
            assert wrote >= 4  # 2 windows × 2 sources

            rows = session.execute(
                text(
                    "SELECT insight_type, value, meta_info "
                    "FROM insights WHERE item_id = :i "
                    "ORDER BY insight_type, (meta_info->>'source_name')"
                ),
                {"i": item_id},
            ).mappings().all()

            types = {r["insight_type"] for r in rows}
            assert "moving_avg_7d" in types
            assert "moving_avg_30d" in types
            for r in rows:
                assert r["meta_info"]["source_name"] in {
                    "steam_market",
                    "skinport",
                }
                assert r["value"] is not None


@_db_required
class TestCrossSource:
    def test_view_and_spread(
        self, sentinel_item, all_known_sources_enabled
    ):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 7, 2, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            skinport_id = _source_id(session, "skinport")
            # Both sources have a fresh price.
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(minutes=10), 42.00,
            )
            _insert_price(
                session, item_id, skinport_id,
                now - timedelta(minutes=10), 28.00,
            )
            session.commit()

            wrote = cross_source.compute_and_store(session, now=now)
            # 1 view + 1 spread row.
            assert wrote == 2

            view = session.execute(
                text(
                    "SELECT meta_info FROM insights "
                    "WHERE item_id=:i AND insight_type='cross_source_view'"
                ),
                {"i": item_id},
            ).scalar_one()
            sources = {s["source_name"]: s for s in view["sources"]}
            assert "steam_market" in sources
            assert "skinport" in sources
            assert sources["steam_market"]["denomination"] == "wallet_credit"
            assert sources["skinport"]["denomination"] == "usd"

            spread = session.execute(
                text(
                    "SELECT value, meta_info FROM insights "
                    "WHERE item_id=:i AND insight_type='cross_source_spread'"
                ),
                {"i": item_id},
            ).mappings().first()
            # Steam is the higher price; spread is positive.
            assert float(spread["value"]) == pytest.approx(0.5, abs=0.05)

    def test_skips_stale_observations(self, sentinel_item):
        """Old prices outside LATEST_PRICE_LOOKBACK should not appear in
        the cross-source view."""
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 7, 3, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(days=2), 99.99,
            )
            session.commit()

            wrote = cross_source.compute_and_store(session, now=now)
            # No fresh price → no rows.
            assert wrote == 0


@_db_required
class TestAnomalyDetection:
    def test_volume_anomaly_fires_on_z_excess(self, sentinel_item):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 7, 4, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            # Baseline: 12 readings at volume=100±tiny.
            for i in range(12):
                _insert_price(
                    session, item_id, steam_id,
                    now - timedelta(days=6) + timedelta(hours=i * 2),
                    Decimal("10.00"),
                    volume=100 + (i % 3),
                )
            # Latest: 5x baseline volume — clear anomaly.
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(minutes=5),
                Decimal("10.00"),
                volume=1000,
            )
            session.commit()

            wrote = anomaly_detection.compute_and_store(session, now=now)
            assert wrote >= 1

            row = session.execute(
                text(
                    "SELECT value, meta_info FROM insights "
                    "WHERE item_id=:i AND insight_type='volume_anomaly'"
                ),
                {"i": item_id},
            ).mappings().first()
            assert row is not None
            assert abs(float(row["value"])) >= 2.0


class TestNarrative:
    """Mock-only — exercises the prompt-assembly + storage logic without
    a real Ollama dependency."""

    @_db_required
    def test_no_newsworthy_data_skips_llm(self, sentinel_item):
        engine = get_engine()
        with Session(engine) as session:
            # Patch chat so a leak would fail loudly.
            with patch(
                "analytics.narrative.chat",
                side_effect=AssertionError("chat must not be called"),
            ):
                wrote = narrative.generate_and_store(
                    session, now=datetime(2099, 7, 5, tzinfo=UTC)
                )
            assert wrote is False

    @_db_required
    def test_stores_row_with_meta_citation(self, sentinel_item):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 7, 6, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            # Two observations 24h apart → movers query returns one row.
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(hours=20), 10.00,
            )
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(minutes=5), 20.00,
            )
            session.commit()

            with patch(
                "analytics.narrative.chat",
                return_value=(
                    "  The sentinel item doubled on Steam wallet credit  "
                ),
            ):
                wrote = narrative.generate_and_store(session, now=now)
            assert wrote is True

            row = session.execute(
                text(
                    "SELECT text_value, meta_info FROM insights "
                    "WHERE insight_type='daily_narrative' "
                    "ORDER BY computed_at DESC LIMIT 1"
                )
            ).mappings().first()
            assert row is not None
            assert "Steam wallet credit" in row["text_value"]
            # meta_info carries the citation payload.
            assert "top_movers" in row["meta_info"]
            assert isinstance(row["meta_info"]["top_movers"], list)
            # Cleanup the inserted narrative row so the next test sees
            # an empty narrative history.
            session.execute(
                text(
                    "DELETE FROM insights WHERE insight_type='daily_narrative' "
                    "AND computed_at = :t"
                ),
                {"t": now},
            )
            session.commit()

    @_db_required
    def test_ollama_error_silent_skip(self, sentinel_item):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 7, 7, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(hours=20), 10.00,
            )
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(minutes=5), 20.00,
            )
            session.commit()

            with patch(
                "analytics.narrative.chat",
                side_effect=OllamaError("simulated unreachable"),
            ):
                wrote = narrative.generate_and_store(session, now=now)
            assert wrote is False
