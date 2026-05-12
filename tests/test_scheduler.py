"""Tests for the scheduler layer.

Coverage:
- ``should_write_observation`` (conditional-write logic): no-prior-row,
  identical, price-change, volume-change, missing-price.
- The Steam/Skinport job wrappers swallow exceptions so the scheduler
  itself never crashes.
- ``build_scheduler`` returns an APScheduler with the two expected jobs
  configured per ADR 009.

Skips the DB-dependent tests when DATABASE_URL is unset or postgres is
unreachable, same pattern as ``test_db_roundtrip.py`` /
``test_migration_roundtrip.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from collectors.base import (
    PriceObservation,
    should_write_observation,
)
from collectors.scheduler import (
    build_scheduler,
    run_skinport_cycle,
    run_steam_cycle,
)
from db.connection import get_engine
from db.models import Item, Price, Source

_TEST_ITEM_NAME = "AK-47 | Redline (Field-Tested)"
# Far-future timestamp so test rows can't collide with real Steam/Skinport
# observations that ever existed or might be inserted in parallel.
_TEST_TIMESTAMP = datetime(2099, 1, 1, tzinfo=UTC)


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


def _make_obs(
    price: str | None, volume: int | None, *, source: str = "steam_market"
) -> PriceObservation:
    return PriceObservation(
        market_hash_name=_TEST_ITEM_NAME,
        source_name=source,
        timestamp=datetime.now(UTC),
        price=Decimal(price) if price is not None else None,
        volume=volume,
        currency="USD",
        raw_response={"test": True},
    )


@pytest.fixture
def session_with_baseline_row():
    """Insert one ``prices`` row at a far-future timestamp for the test
    item + steam_market source. Yields a Session; cleans up the test row
    afterward so production data is preserved."""
    engine = get_engine()
    with Session(engine) as session:
        item_id = session.execute(
            select(Item.id).where(
                Item.market_hash_name == _TEST_ITEM_NAME
            )
        ).scalar_one()
        source_id = session.execute(
            select(Source.id).where(Source.name == "steam_market")
        ).scalar_one()

        # Wipe any leftover from a previous interrupted run.
        session.execute(
            text(
                "DELETE FROM prices WHERE item_id = :i AND source_id = :s "
                "AND timestamp >= :t"
            ),
            {
                "i": item_id,
                "s": source_id,
                "t": _TEST_TIMESTAMP - timedelta(days=1),
            },
        )

        stmt = (
            pg_insert(Price)
            .values(
                item_id=item_id,
                source_id=source_id,
                timestamp=_TEST_TIMESTAMP,
                price=Decimal("10.00"),
                volume=100,
                currency="USD",
                raw_response={"baseline": True},
            )
            .on_conflict_do_nothing(
                index_elements=["item_id", "source_id", "timestamp"]
            )
        )
        session.execute(stmt)
        session.commit()

        try:
            yield session
        finally:
            session.execute(
                text(
                    "DELETE FROM prices WHERE item_id = :i "
                    "AND source_id = :s AND timestamp >= :t"
                ),
                {
                    "i": item_id,
                    "s": source_id,
                    "t": _TEST_TIMESTAMP - timedelta(days=1),
                },
            )
            session.commit()


@_db_required
class TestShouldWriteObservation:
    def test_returns_true_when_no_prior_row(self) -> None:
        """For an item that has never been observed at this source, write."""
        engine = get_engine()
        with Session(engine) as session:
            # Use a SOURCE that has no rows for the test item to guarantee
            # "no prior row" — skinport may have many rows for this item
            # from real collection, but the combination
            # (test item, brand-new fake source) won't.
            obs = _make_obs("99.99", 1, source="__nonexistent_source__")
            assert should_write_observation(session, obs) is True

    def test_returns_false_when_identical(
        self, session_with_baseline_row
    ) -> None:
        """Same price and same volume as the most recent row → skip."""
        obs = _make_obs("10.00", 100)
        assert should_write_observation(session_with_baseline_row, obs) is False

    def test_returns_true_on_price_change(
        self, session_with_baseline_row
    ) -> None:
        obs = _make_obs("10.01", 100)
        assert should_write_observation(session_with_baseline_row, obs) is True

    def test_returns_true_on_volume_change(
        self, session_with_baseline_row
    ) -> None:
        obs = _make_obs("10.00", 101)
        assert should_write_observation(session_with_baseline_row, obs) is True

    def test_returns_true_on_both_changed(
        self, session_with_baseline_row
    ) -> None:
        obs = _make_obs("11.00", 50)
        assert should_write_observation(session_with_baseline_row, obs) is True

    def test_returns_false_when_price_is_none(self) -> None:
        """A None-price observation is unwritable regardless of history."""
        engine = get_engine()
        with Session(engine) as session:
            obs = _make_obs(None, 100)
            assert should_write_observation(session, obs) is False


class TestCycleWrappers:
    """The Steam/Skinport job wrappers must not let exceptions escape —
    APScheduler treats an uncaught exception in a job as a job failure
    that doesn't stop the scheduler, but logging gets garbled and the
    user-visible cycle summary line goes missing. We log+swallow instead."""

    def test_steam_cycle_swallows_exception(self) -> None:
        with patch(
            "collectors.scheduler._run_cycle",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            # Should not raise.
            run_steam_cycle()

    def test_skinport_cycle_swallows_exception(self) -> None:
        with patch(
            "collectors.scheduler._run_cycle",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            run_skinport_cycle()


class TestSchedulerConfig:
    """``build_scheduler`` returns a configured-but-not-started scheduler;
    no thread/resource cleanup is needed in these tests."""

    def test_two_jobs_registered_with_expected_intervals(self) -> None:
        scheduler = build_scheduler()
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {"steam_cycle", "skinport_cycle"}
        # APScheduler's IntervalTrigger exposes the interval as a
        # timedelta on the trigger object.
        assert jobs["steam_cycle"].trigger.interval == timedelta(minutes=30)
        assert jobs["skinport_cycle"].trigger.interval == timedelta(minutes=5)

    def test_scheduler_has_overlap_defaults(self) -> None:
        """The overlap/coalesce/grace-time policy from ADR 009 must be in
        the scheduler's job_defaults so every added job picks them up.
        Checking the defaults dict rather than per-Job attributes because
        APScheduler 3.x doesn't expose those as public attributes on Job.
        """
        scheduler = build_scheduler()
        defaults = scheduler._job_defaults  # stable internal across 3.x
        assert defaults.get("max_instances") == 1
        assert defaults.get("coalesce") is True
        assert defaults.get("misfire_grace_time") == 300
