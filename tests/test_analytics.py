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
    unavailability_streak,
)
from analytics.ollama_client import OllamaError
from analytics.unavailability_streak import GRACE_FACTOR
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


def _upsert_observation_log(
    session, item_id, source_id, last_observed_at
) -> None:
    """Test helper: stamp observation_log directly with a known
    timestamp. Mirrors what ``collectors.base.update_observation_log``
    does at runtime; tests control the timestamp explicitly."""
    session.execute(
        text(
            """
            INSERT INTO observation_log (item_id, source_id, last_observed_at)
            VALUES (:item_id, :source_id, :ts)
            ON CONFLICT (item_id, source_id)
            DO UPDATE SET last_observed_at = EXCLUDED.last_observed_at
            """
        ),
        {
            "item_id": item_id,
            "source_id": source_id,
            "ts": last_observed_at,
        },
    )


@_db_required
class TestUnavailabilityStreak:
    """Per-(item, source) streak counter for items missing from a source
    across consecutive analytics cycles. Sparse storage: rows emitted
    only for currently-missing pairs.

    Tests stamp observation_log directly (the production signal); the
    Phase 7a refactor moved streak compute off ``prices`` (which is
    dedup-filtered) onto ``observation_log`` (advanced pre-dedup).
    """

    def _streak_row(
        self, session, item_id, source_name: str
    ) -> dict | None:
        return (
            session.execute(
                text(
                    """
                    SELECT value, meta_info
                    FROM insights
                    WHERE item_id = :i
                      AND insight_type = 'item_unavailability_streak'
                      AND meta_info->>'source_name' = :s
                    ORDER BY computed_at DESC
                    LIMIT 1
                    """
                ),
                {"i": item_id, "s": source_name},
            )
            .mappings()
            .first()
        )

    @pytest.fixture(autouse=True)
    def _cleanup_streak_artifacts(self, sentinel_item):
        """``compute_and_store`` writes rows for ALL items in the DB,
        not just the sentinel. Wipe streak rows at the test's
        far-future timestamps (and any observation_log rows we
        synthetically stamp for the sentinel) before AND after to
        keep the live DB faithful.
        """
        engine = get_engine()

        def _wipe():
            with Session(engine) as session:
                session.execute(
                    text(
                        "DELETE FROM insights "
                        "WHERE insight_type = 'item_unavailability_streak' "
                        "AND computed_at >= TIMESTAMPTZ '2099-01-01'"
                    )
                )
                session.execute(
                    text(
                        "DELETE FROM observation_log "
                        "WHERE item_id = :i"
                    ),
                    {"i": sentinel_item},
                )
                session.commit()

        _wipe()
        yield
        _wipe()

    def test_fresh_observation_emits_no_row(
        self, sentinel_item, all_known_sources_enabled
    ):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 8, 1, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            # Observation is well inside the grace window (60min × 1.5).
            _upsert_observation_log(
                session, item_id, steam_id,
                now - timedelta(minutes=5),
            )
            session.commit()

            unavailability_streak.compute_and_store(session, now=now)
            session.commit()

            assert (
                self._streak_row(session, item_id, "steam_market")
                is None
            )

    def test_missing_for_one_cycle_emits_streak_1(
        self, sentinel_item, all_known_sources_enabled
    ):
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 8, 1, tzinfo=UTC)
        last_obs_at = now - timedelta(hours=2)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            # Steam: 60min interval × 1.5 = 90min grace; last poll 2h
            # ago is comfortably outside grace.
            _upsert_observation_log(
                session, item_id, steam_id, last_obs_at
            )
            session.commit()

            unavailability_streak.compute_and_store(session, now=now)
            session.commit()

            row = self._streak_row(session, item_id, "steam_market")

        assert row is not None
        assert int(row["value"]) == 1
        meta = row["meta_info"]
        assert meta["source_name"] == "steam_market"
        assert meta["streak_cycles"] == 1
        assert meta["last_seen_observed"] == last_obs_at.isoformat()
        assert meta["first_seen_unavailable"] == now.isoformat()

    def test_continuation_increments_streak(
        self, sentinel_item, all_known_sources_enabled
    ):
        engine = get_engine()
        item_id = sentinel_item
        t1 = datetime(2099, 8, 1, 10, tzinfo=UTC)
        t2 = t1 + timedelta(hours=1)
        t3 = t2 + timedelta(hours=1)
        last_obs_at = t1 - timedelta(hours=4)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            _upsert_observation_log(
                session, item_id, steam_id, last_obs_at
            )
            session.commit()

            unavailability_streak.compute_and_store(session, now=t1)
            unavailability_streak.compute_and_store(session, now=t2)
            unavailability_streak.compute_and_store(session, now=t3)
            session.commit()

            row = self._streak_row(session, item_id, "steam_market")

        # Three consecutive missing cycles → streak == 3.
        assert int(row["value"]) == 3
        # first_seen_unavailable carried forward from the first cycle.
        assert row["meta_info"]["first_seen_unavailable"] == t1.isoformat()

    def test_intervening_observation_resets_streak(
        self, sentinel_item, all_known_sources_enabled
    ):
        engine = get_engine()
        item_id = sentinel_item
        t1 = datetime(2099, 8, 2, 10, tzinfo=UTC)
        t2 = t1 + timedelta(hours=1)
        t3_late = t2 + timedelta(hours=2)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")

            # Round 1: stale observation → streak=1
            _upsert_observation_log(
                session, item_id, steam_id,
                t1 - timedelta(hours=4),
            )
            session.commit()
            unavailability_streak.compute_and_store(session, now=t1)
            session.commit()

            # Round 2: a fresh observation arrives — no streak row.
            _upsert_observation_log(
                session, item_id, steam_id,
                t2 - timedelta(minutes=5),
            )
            session.commit()
            unavailability_streak.compute_and_store(session, now=t2)
            session.commit()

            # Round 3: item missing again (last obs ~115min old).
            unavailability_streak.compute_and_store(session, now=t3_late)
            session.commit()

            row = self._streak_row(session, item_id, "steam_market")

        # The streak should have RESET to 1 — the intervening
        # observation at round 2 broke the continuity from round 1.
        assert int(row["value"]) == 1
        assert (
            row["meta_info"]["first_seen_unavailable"]
            == t3_late.isoformat()
        )

    def test_never_observed_item_gets_streak_with_null_last_seen(
        self, sentinel_item, all_known_sources_enabled
    ):
        """A new watchlist item with no observations on a source yet
        still emits a streak row — last_seen_observed=null signals
        the bot can render 'never observed' specifically."""
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 8, 3, tzinfo=UTC)

        with Session(engine) as session:
            unavailability_streak.compute_and_store(session, now=now)
            session.commit()
            row = self._streak_row(session, item_id, "steam_market")

        assert row is not None
        assert int(row["value"]) == 1
        assert row["meta_info"]["last_seen_observed"] is None

    def test_dedup_observation_counts_as_fresh(
        self, sentinel_item, all_known_sources_enabled
    ):
        """REGRESSION (Phase 7a checkpoint): the original streak design
        read MAX(prices.timestamp), which doesn't advance when the
        collector's price is dedup'd. observation_log fixes this — a
        dedup'd observation still updates observation_log, so the
        streak compute treats it as fresh.

        Setup: prices.timestamp is OLD (simulating "price hasn't
        changed in days" dedup state), but observation_log is RECENT
        (the collector just polled and saw the same price). The
        streak compute must emit NO row.
        """
        engine = get_engine()
        item_id = sentinel_item
        now = datetime(2099, 8, 4, tzinfo=UTC)

        with Session(engine) as session:
            steam_id = _source_id(session, "steam_market")
            # prices: ancient — simulates a long dedup'd run.
            _insert_price(
                session, item_id, steam_id,
                now - timedelta(days=30), 42.00,
            )
            # observation_log: fresh — the collector polled 5min ago
            # and saw the same (42.00, vol). Dedup skipped the write,
            # but observation_log is updated.
            _upsert_observation_log(
                session, item_id, steam_id,
                now - timedelta(minutes=5),
            )
            session.commit()

            unavailability_streak.compute_and_store(session, now=now)
            session.commit()

            row = self._streak_row(session, item_id, "steam_market")

        assert row is None, (
            "Dedup'd-but-recently-polled item must NOT be flagged as "
            "unavailable. The pre-fix design used MAX(prices.timestamp) "
            "which would see the 30-day-old write and emit a streak."
        )

    def test_grace_factor_constant(self):
        """Sanity-check the constant didn't drift accidentally — ADR 015
        documents the 1.5× choice."""
        assert GRACE_FACTOR == 1.5
