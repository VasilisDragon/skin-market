"""Tests for the scheduler layer.

Coverage:
- ``should_write_observation`` (conditional-write logic).
- Job wrappers swallow exceptions so the scheduler never crashes from a
  cycle error.
- ``build_scheduler`` registers one job per enabled source with the
  cadence read from the ``sources`` table (or from an injected list).
- ``compute_pause_seconds`` honors Retry-After when present and uses
  the doubling fallback ladder otherwise.
- ``_run_cycle`` cycle-level heuristic: an outsized fraction of empty
  outcomes re-labels ambiguous Nones as declined.

DB-dependent tests skip when DATABASE_URL is unset / postgres is
unreachable, same pattern as ``test_db_roundtrip.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from collectors.base import (
    DECLINED,
    Collector,
    PriceObservation,
    RateLimited,
    should_write_observation,
)
from collectors.scheduler import (
    AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD,
    RATE_LIMIT_FALLBACK_CAP_SECONDS,
    RATE_LIMIT_FALLBACK_INITIAL_SECONDS,
    SourceJobSpec,
    _active_source_specs,
    _load_enabled_sources,
    _rate_limit_state,
    _run_cycle,
    build_scheduler,
    compute_pause_seconds,
    run_skinport_cycle,
    run_steam_cycle,
)
from db.connection import get_engine
from db.models import Item, Price, Source

# Sentinel item used exclusively by this test module. A name no real
# CS2 item could ever match (the double-underscore prefix + bracket
# pattern is unmistakable). Was previously a real watchlist item
# ("AK-47 | Redline (Field-Tested)"); see
# ``docs/pre-phase2-diagnostics.md §Q2`` for the contamination story
# — the ``_FakeCollector`` in ``TestRunCycleDeclinedHeuristic``
# bypasses ``SteamCollector.collect_one`` (and therefore the outlier
# filter), so any rows it produced landed in the live ``prices``
# table at face value against the real Redline item. The bot then
# reported Steam = $1.00 SC for Redline in live Discord output.
_TEST_ITEM_NAME = "__SchedulerTest__ | Sentinel (Field-Tested)"
_TEST_ITEM_SLUG = "schedulertest-sentinel-field-tested"
# Far-future timestamp so test rows can't collide with real Steam/Skinport
# observations.
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


def _purge_sentinel(session: Session) -> None:
    """Delete every row pointing at the sentinel item (prices,
    observation_log, insights, then the item itself). Idempotent."""
    item_id = session.execute(
        select(Item.id).where(Item.market_hash_name == _TEST_ITEM_NAME)
    ).scalar_one_or_none()
    if item_id is None:
        return
    session.execute(
        text("DELETE FROM prices WHERE item_id = :i"), {"i": item_id}
    )
    session.execute(
        text("DELETE FROM observation_log WHERE item_id = :i"),
        {"i": item_id},
    )
    session.execute(
        text("DELETE FROM insights WHERE item_id = :i"), {"i": item_id}
    )
    session.execute(
        text("DELETE FROM items WHERE id = :i"), {"i": item_id}
    )


@pytest.fixture(autouse=True, scope="module")
def _ensure_sentinel_item():
    """Ensure the sentinel item exists in the DB for the lifetime of
    this module, and purge every row pointing at it on teardown.

    Previously this module used the real watchlist item AK-47 Redline
    FT as its hardcoded test target. ``_FakeCollector`` bypasses
    ``SteamCollector.collect_one`` (and the outlier filter), so
    ``_run_cycle`` happily persisted synthetic
    ``raw_response={"test": True}`` rows against the real Redline
    item, and the bot reported Steam = $1.00 SC for Redline in live
    Discord. See ``docs/pre-phase2-diagnostics.md §Q2`` for the full
    trace.

    Skips cleanly when the DB is unreachable; the surrounding
    ``_db_required`` marker keeps DB-dependent tests deselected in
    that case.
    """
    if not _db_reachable():
        yield
        return
    engine = get_engine()
    with Session(engine) as session:
        # Up-front purge — defensive in case a prior aborted run left
        # rows behind.
        _purge_sentinel(session)
        session.execute(
            text(
                """
                INSERT INTO items (
                    market_hash_name, display_name, slug, item_type,
                    weapon_name, skin_name, wear
                )
                VALUES (
                    :name, :name, :slug, 'rifle',
                    'TestWeapon', 'Sentinel', 'Field-Tested'
                )
                ON CONFLICT (market_hash_name) DO NOTHING
                """
            ),
            {"name": _TEST_ITEM_NAME, "slug": _TEST_ITEM_SLUG},
        )
        session.commit()
    yield
    with Session(engine) as session:
        _purge_sentinel(session)
        session.commit()


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
            select(Item.id).where(Item.market_hash_name == _TEST_ITEM_NAME)
        ).scalar_one()
        source_id = session.execute(
            select(Source.id).where(Source.name == "steam_market")
        ).scalar_one()

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


@pytest.fixture
def _active_specs():
    """Populate ``_active_source_specs`` so ``run_<source>_cycle`` wrappers
    reach the body of ``_run_cycle`` instead of short-circuiting on a
    missing spec. Restores the prior state after the test."""
    snapshot = dict(_active_source_specs)
    _active_source_specs.clear()
    _active_source_specs.update(
        {
            "steam_market": SourceJobSpec("steam_market", 60, 5),
            "skinport": SourceJobSpec("skinport", 15, 0),
            "dmarket": SourceJobSpec("dmarket", 15, 3),
        }
    )
    try:
        yield
    finally:
        _active_source_specs.clear()
        _active_source_specs.update(snapshot)


@pytest.fixture(autouse=True)
def _clean_rate_limit_state():
    """Wipe the rate-limit memory between tests so the doubling ladder
    starts from a known state for every compute_pause_seconds test."""
    _rate_limit_state.clear()
    yield
    _rate_limit_state.clear()


@_db_required
class TestShouldWriteObservation:
    def test_returns_true_when_no_prior_row(self) -> None:
        engine = get_engine()
        with Session(engine) as session:
            obs = _make_obs("99.99", 1, source="__nonexistent_source__")
            assert should_write_observation(session, obs) is True

    def test_returns_false_when_identical(
        self, session_with_baseline_row
    ) -> None:
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
        engine = get_engine()
        with Session(engine) as session:
            obs = _make_obs(None, 100)
            assert should_write_observation(session, obs) is False


class TestCycleWrappers:
    """The Steam/Skinport job wrappers must not let exceptions escape —
    APScheduler treats an uncaught exception in a job as a job failure
    that doesn't stop the scheduler, but logging gets garbled and the
    user-visible cycle summary line goes missing. We log+swallow instead."""

    def test_steam_cycle_swallows_exception(self, _active_specs) -> None:
        with patch(
            "collectors.scheduler._run_cycle",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            run_steam_cycle()  # should not raise

    def test_skinport_cycle_swallows_exception(
        self, _active_specs
    ) -> None:
        with patch(
            "collectors.scheduler._run_cycle",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            run_skinport_cycle()  # should not raise

    def test_cycle_short_circuits_when_source_not_registered(self) -> None:
        # _active_source_specs is empty (no _active_specs fixture); the
        # wrapper logs a warning and returns without touching _run_cycle.
        with patch(
            "collectors.scheduler._run_cycle",
            side_effect=RuntimeError("should never be called"),
        ):
            run_steam_cycle()


class TestBuildSchedulerInjection:
    """Tests that don't need the DB — they inject SourceJobSpec lists."""

    def test_jobs_registered_with_expected_intervals(self) -> None:
        specs = [
            SourceJobSpec("steam_market", 60, 5),
            SourceJobSpec("skinport", 15, 0),
            SourceJobSpec("dmarket", 15, 3),
        ]
        scheduler = build_scheduler(source_jobs=specs)
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {
            "steam_market_cycle",
            "skinport_cycle",
            "dmarket_cycle",
        }
        assert (
            jobs["steam_market_cycle"].trigger.interval
            == timedelta(minutes=60)
        )
        assert (
            jobs["skinport_cycle"].trigger.interval
            == timedelta(minutes=15)
        )
        assert (
            jobs["dmarket_cycle"].trigger.interval
            == timedelta(minutes=15)
        )

    def test_scheduler_has_overlap_defaults(self) -> None:
        scheduler = build_scheduler(source_jobs=[])
        defaults = scheduler._job_defaults  # stable internal across 3.x
        assert defaults.get("max_instances") == 1
        assert defaults.get("coalesce") is True
        assert defaults.get("misfire_grace_time") == 300

    def test_empty_specs_registers_no_jobs(self) -> None:
        scheduler = build_scheduler(source_jobs=[])
        assert scheduler.get_jobs() == []

    def test_unknown_source_name_is_skipped(self) -> None:
        scheduler = build_scheduler(
            source_jobs=[
                SourceJobSpec("steam_market", 60, 5),
                SourceJobSpec("totally_made_up_source", 5, 0),
            ]
        )
        ids = {job.id for job in scheduler.get_jobs()}
        assert ids == {"steam_market_cycle"}

    def test_reads_interval_from_spec(self) -> None:
        """The interval value baked into the trigger reflects the spec,
        not a hardcoded constant — this is the test the user's addendum
        called out."""
        scheduler = build_scheduler(
            source_jobs=[SourceJobSpec("steam_market", 42, 5)]
        )
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert (
            jobs["steam_market_cycle"].trigger.interval
            == timedelta(minutes=42)
        )

    def test_rebuild_picks_up_new_interval(self) -> None:
        """Optional integration test from the plan: a subsequent
        build_scheduler call with a different interval propagates to
        the new scheduler instance, exercising the full DB-read /
        rebuild story without actually touching the DB."""
        first = build_scheduler(
            source_jobs=[SourceJobSpec("steam_market", 30, 5)]
        )
        assert (
            {j.id: j for j in first.get_jobs()}[
                "steam_market_cycle"
            ].trigger.interval
            == timedelta(minutes=30)
        )
        second = build_scheduler(
            source_jobs=[SourceJobSpec("steam_market", 90, 5)]
        )
        assert (
            {j.id: j for j in second.get_jobs()}[
                "steam_market_cycle"
            ].trigger.interval
            == timedelta(minutes=90)
        )


@_db_required
class TestBuildSchedulerFromDB:
    """Default build_scheduler() reads ``sources WHERE enabled = TRUE``.

    The live DB's ``enabled`` flag is the single switch — disabling a
    source via ``UPDATE sources SET enabled = FALSE WHERE name = ...``
    removes its job from the next ``build_scheduler``."""

    def test_disabled_source_not_scheduled(self) -> None:
        engine = get_engine()
        # Pick a currently-enabled, *independently-scheduled* source,
        # flip it disabled, assert build_scheduler omits it. The
        # Pricempire sub-providers (pricempire_buff163 etc.) are
        # enabled but explicitly NOT independently scheduled — they
        # share the `pricempire` pseudo-source's job (ADR 018/019).
        # Exclude them from this test.
        with Session(engine) as session:
            enabled_names = [
                row[0]
                for row in session.execute(
                    select(Source.name)
                    .where(Source.enabled.is_(True))
                    .order_by(Source.id)
                ).all()
                if not row[0].startswith("pricempire_")
            ]
            if not enabled_names:
                pytest.skip("no enabled sources in test DB")
            victim = enabled_names[0]
            session.execute(
                text(
                    "UPDATE sources SET enabled = FALSE WHERE name = :n"
                ),
                {"n": victim},
            )
            session.commit()
            try:
                scheduler = build_scheduler()
                ids = {job.id for job in scheduler.get_jobs()}
                assert f"{victim}_cycle" not in ids
                # Other previously-enabled, independently-scheduled
                # sources still scheduled.
                for other in enabled_names[1:]:
                    assert f"{other}_cycle" in ids
            finally:
                session.execute(
                    text(
                        "UPDATE sources SET enabled = TRUE "
                        "WHERE name = :n"
                    ),
                    {"n": victim},
                )
                session.commit()

    def test_load_enabled_sources_excludes_disabled_row(self) -> None:
        engine = get_engine()
        with Session(engine) as session:
            enabled_names = [
                row[0]
                for row in session.execute(
                    select(Source.name).where(Source.enabled.is_(True))
                ).all()
            ]
            if not enabled_names:
                pytest.skip("no enabled sources in test DB")
            victim = enabled_names[0]
            session.execute(
                text(
                    "UPDATE sources SET enabled = FALSE WHERE name = :n"
                ),
                {"n": victim},
            )
            session.commit()
            try:
                specs = _load_enabled_sources(session)
                names = {s.name for s in specs}
                assert victim not in names
            finally:
                session.execute(
                    text(
                        "UPDATE sources SET enabled = TRUE "
                        "WHERE name = :n"
                    ),
                    {"n": victim},
                )
                session.commit()


class TestComputePauseSeconds:
    """The pause-decision logic for 429 retry-exhaustion.

    Pure function modulo module-level ``_rate_limit_state`` updates.
    The ``_clean_rate_limit_state`` autouse fixture resets that state
    between tests."""

    def test_retry_after_header_used_directly(self) -> None:
        pause = compute_pause_seconds("steam_market", 60)
        assert pause == 60

    def test_retry_after_zero_is_used(self) -> None:
        # Zero is technically a valid Retry-After (server says "okay
        # now"); we honor it rather than falling through to the 5-min
        # ladder.
        pause = compute_pause_seconds("steam_market", 0)
        assert pause == 0

    def test_fallback_initial_pause(self) -> None:
        pause = compute_pause_seconds("steam_market", None)
        assert pause == RATE_LIMIT_FALLBACK_INITIAL_SECONDS

    def test_fallback_doubles_within_window(self) -> None:
        now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
        first = compute_pause_seconds("steam_market", None, now=now)
        second = compute_pause_seconds(
            "steam_market", None, now=now + timedelta(minutes=2)
        )
        assert first == 300
        assert second == 600

    def test_fallback_resets_after_window(self) -> None:
        now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
        compute_pause_seconds("steam_market", None, now=now)
        # >1 hour later — memory ages out, ladder resets to initial.
        later = now + timedelta(hours=2)
        pause = compute_pause_seconds("steam_market", None, now=later)
        assert pause == RATE_LIMIT_FALLBACK_INITIAL_SECONDS

    def test_fallback_caps_at_one_hour(self) -> None:
        now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
        pauses = []
        # Many consecutive 429s within the same window.
        for i in range(20):
            pauses.append(
                compute_pause_seconds(
                    "steam_market",
                    None,
                    now=now + timedelta(seconds=i * 30),
                )
            )
        # Eventually saturates at the cap.
        assert pauses[-1] == RATE_LIMIT_FALLBACK_CAP_SECONDS
        # And never exceeds it.
        assert max(pauses) == RATE_LIMIT_FALLBACK_CAP_SECONDS

    def test_state_keyed_by_source(self) -> None:
        # Doubling for one source doesn't bleed into another.
        now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
        compute_pause_seconds("steam_market", None, now=now)
        skinport_first = compute_pause_seconds(
            "skinport", None, now=now + timedelta(seconds=1)
        )
        assert skinport_first == RATE_LIMIT_FALLBACK_INITIAL_SECONDS


class _FakeCollector(Collector):
    """Test double that yields a scripted sequence of outcomes.

    Wraps the scripted list in a fake make_client so collect_cycle
    follows the standard control flow (with-block enter/exit on the
    client) but the HTTP layer is never reached.
    """

    source_name = "fake_source"

    def __init__(
        self,
        scripted: list,
        *,
        raise_at_end: RateLimited | None = None,
    ) -> None:
        self._scripted = scripted
        self._raise_at_end = raise_at_end

    def collect_one(self, client, market_hash_name):  # type: ignore[override]
        raise NotImplementedError("collect_cycle is overridden")

    def collect_cycle(self, client, market_hash_names):  # type: ignore[override]
        names = list(market_hash_names)
        for i, _name in enumerate(names):
            if i < len(self._scripted):
                yield self._scripted[i]
            else:
                yield None
        if self._raise_at_end is not None:
            raise self._raise_at_end

    def make_client(self):  # type: ignore[override]
        from contextlib import nullcontext

        return nullcontext(enter_result=None)


def _fake_watchlist_size() -> int:
    from collectors.scheduler import _load_watchlist

    with Session(get_engine()) as session:
        return len(_load_watchlist(session, source_name="fake_source"))


@_db_required
class TestRunCycleDeclinedHeuristic:
    """The cycle-level heuristic relabels ambiguous Nones as declined
    when an outsized fraction of a cycle came back empty. Threshold is
    ``AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD`` (0.5)."""

    def test_below_threshold_keeps_unavailable_label(
        self, caplog
    ) -> None:
        # Need a watchlist; _run_cycle queries items. Build a small
        # synthetic outcome list whose length is <= the real watchlist;
        # the slicing in _load_watchlist preserves order. We script as
        # many outcomes as the watchlist length.
        watchlist_size = _fake_watchlist_size()
        # Few Nones, mostly written (PriceObservations) — well below
        # the 50% threshold.
        scripted = [_make_obs("1.00", 1) for _ in range(watchlist_size - 2)]
        scripted += [None, None]
        collector = _FakeCollector(scripted)
        with caplog.at_level("INFO", logger="collectors.scheduler"):
            _run_cycle(collector, "Fake")
        cycle_complete_lines = [
            r for r in caplog.records if "cycle complete" in r.getMessage()
        ]
        assert cycle_complete_lines, "missing cycle complete log line"
        msg = cycle_complete_lines[-1].getMessage()
        # 2 ambiguous Nones, below threshold — counted as unavailable.
        assert "2 unavailable, 0 declined" in msg

    def test_above_threshold_relabels_nones_as_declined(
        self, caplog
    ) -> None:
        watchlist_size = _fake_watchlist_size()
        # >50% Nones — cycle marked degraded, all Nones become declined.
        none_count = (watchlist_size // 2) + 2  # comfortably above 50%
        scripted = [None] * none_count + [
            _make_obs("1.00", 1)
            for _ in range(watchlist_size - none_count)
        ]
        collector = _FakeCollector(scripted)
        with caplog.at_level("INFO", logger="collectors.scheduler"):
            _run_cycle(collector, "Fake")
        msg = next(
            r.getMessage()
            for r in caplog.records
            if "cycle complete" in r.getMessage()
        )
        # All ambiguous Nones converted to declined; none remain
        # unavailable.
        assert "0 unavailable" in msg
        assert f"{none_count} declined" in msg

    def test_explicit_declined_always_counted(self, caplog) -> None:
        watchlist_size = _fake_watchlist_size()
        # Mostly DECLINED — even without ambiguous Nones to relabel,
        # the declined counter should reflect explicit DECLINEDs.
        scripted = [DECLINED] * watchlist_size
        collector = _FakeCollector(scripted)
        with caplog.at_level("INFO", logger="collectors.scheduler"):
            _run_cycle(collector, "Fake")
        msg = next(
            r.getMessage()
            for r in caplog.records
            if "cycle complete" in r.getMessage()
        )
        assert f"{watchlist_size} declined" in msg


@_db_required
class TestRunCycleRateLimited:
    """When the collector raises RateLimited mid-cycle, _run_cycle counts
    the partial results, logs, then computes and applies a pause via
    compute_pause_seconds (which updates _rate_limit_state)."""

    def test_partial_results_counted_then_pause_logged(
        self, caplog
    ) -> None:
        watchlist_size = _fake_watchlist_size()
        # Two successful items then RateLimited — cycle should record
        # the partial outcomes and compute a fallback pause.
        scripted = [None, None]
        collector = _FakeCollector(
            scripted,
            raise_at_end=RateLimited("fake_source", None),
        )
        with caplog.at_level("INFO", logger="collectors.scheduler"):
            _run_cycle(collector, "Fake")
        # rate-limit state should now have an entry for fake_source.
        assert "fake_source" in _rate_limit_state
        # And the pause log line should be present.
        assert any(
            "rate-limited" in r.getMessage().lower()
            for r in caplog.records
        )
        # Note: 2 ambiguous Nones out of `watchlist_size` items, but
        # collect_cycle aborts after 2 yields so total outcomes is 2,
        # both Nones — 100% empty → relabeled as declined.
        _ = watchlist_size  # silence linter on the unused size

    def test_retry_after_header_propagates_to_pause(self) -> None:
        collector = _FakeCollector(
            [],
            raise_at_end=RateLimited("fake_source", 137),
        )
        _run_cycle(collector, "Fake")
        # The compute_pause_seconds call should have used 137 directly.
        memory = _rate_limit_state["fake_source"]
        assert memory.current_pause_seconds == 137


class TestAmbiguousThresholdConstant:
    """Sanity-check the threshold constant didn't drift accidentally —
    ADR 013 §3 documents the 0.5 choice; if someone changes the value
    the ADR should be updated alongside."""

    def test_threshold_is_half(self) -> None:
        assert AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD == 0.5


_LW_FIXTURE_YAML = """\
schema_version: 3
sources:
  - { name: skinport, base_url: https://example, rate_limit_per_minute: 60, enabled: true }
items:
  - { market_hash_name: "AK-47 | Deep1 (Field-Tested)", item_type: rifle, tier: curated }
  - { market_hash_name: "AK-47 | Deep2 (Factory New)", item_type: rifle, tier: curated }
  - { market_hash_name: "AWP | Deep3 (Battle-Scarred)", item_type: sniper, tier: curated }
  - { market_hash_name: "Glock-18 | Broad1 (Factory New)", item_type: pistol, tier: featured }
  - { market_hash_name: "MP9 | Broad2 (Field-Tested)", item_type: smg, tier: featured }
"""


class TestLoadWatchlistTierFilter:
    """_load_watchlist filters active items from watchlist.yaml."""

    def _write_yaml(self, tmp_path: Path, body: str = _LW_FIXTURE_YAML) -> Path:
        path = tmp_path / "wl.yaml"
        path.write_text(body)
        return path

    def test_load_watchlist_curated_only_for_steam(
        self, tmp_path: Path
    ) -> None:
        """steam_market polls curated tier only."""
        from collectors.scheduler import _load_watchlist

        path = self._write_yaml(tmp_path)
        names = _load_watchlist(
            None,  # session is unused; see _load_watchlist docstring
            source_name="steam_market",
            watchlist_path=path,
        )
        assert names == [
            "AK-47 | Deep1 (Field-Tested)",
            "AK-47 | Deep2 (Factory New)",
            "AWP | Deep3 (Battle-Scarred)",
        ]

    def test_load_watchlist_curated_only_for_dmarket(
        self, tmp_path: Path
    ) -> None:
        """dmarket polls curated tier only."""
        from collectors.scheduler import _load_watchlist

        path = self._write_yaml(tmp_path)
        names = _load_watchlist(
            None, source_name="dmarket", watchlist_path=path
        )
        assert all("Broad" not in n for n in names)
        assert len(names) == 3

    def test_load_watchlist_curated_plus_featured_for_skinport(
        self, tmp_path: Path
    ) -> None:
        """skinport polls curated and featured tiers."""
        from collectors.scheduler import _load_watchlist

        path = self._write_yaml(tmp_path)
        names = _load_watchlist(
            None, source_name="skinport", watchlist_path=path
        )
        assert set(names) == {
            "AK-47 | Deep1 (Field-Tested)",
            "AK-47 | Deep2 (Factory New)",
            "AWP | Deep3 (Battle-Scarred)",
            "Glock-18 | Broad1 (Factory New)",
            "MP9 | Broad2 (Field-Tested)",
        }

    def test_load_watchlist_omits_substrate(
        self, tmp_path: Path
    ) -> None:
        """Items absent from YAML stay out of the active watchlist."""
        from collectors.scheduler import _load_watchlist

        path = self._write_yaml(tmp_path)
        names = _load_watchlist(
            None, source_name="skinport", watchlist_path=path
        )
        assert all(
            n in {
                "AK-47 | Deep1 (Field-Tested)",
                "AK-47 | Deep2 (Factory New)",
                "AWP | Deep3 (Battle-Scarred)",
                "Glock-18 | Broad1 (Factory New)",
                "MP9 | Broad2 (Field-Tested)",
            }
            for n in names
        )

    def test_load_watchlist_respects_limit_kwarg(
        self, tmp_path: Path
    ) -> None:
        """The limit kwarg slices the post-filter alphabetical list."""
        from collectors.scheduler import _load_watchlist

        path = self._write_yaml(tmp_path)
        names = _load_watchlist(
            None,
            source_name="skinport",
            watchlist_path=path,
            limit=2,
        )
        assert names == [
            "AK-47 | Deep1 (Field-Tested)",
            "AK-47 | Deep2 (Factory New)",
        ]


class TestPricempirePathBypassesLoadWatchlist:
    """Pricempire reads the item table directly, not watchlist.yaml."""

    def test_pricempire_collector_does_not_import_load_watchlist(
        self,
    ) -> None:
        """The Pricempire collector module must NOT import or call
        _load_watchlist."""
        import inspect

        from collectors import pricempire

        source = inspect.getsource(pricempire)
        assert "_load_watchlist" not in source, (
            "collectors/pricempire.py references _load_watchlist; "
            "this breaks the orphan-data-stays-warm invariant "
            "(ADR 024). Pricempire must read items table directly "
            "via _load_item_index, not the YAML-filtered watchlist."
        )

    def test_pricempire_uses_load_item_index(self) -> None:
        """Pricempire keeps the item-table reader contract."""
        import inspect

        from collectors.pricempire import _load_item_index

        params = list(inspect.signature(_load_item_index).parameters)
        assert params == ["session"], (
            f"_load_item_index signature unexpectedly changed; got "
            f"{params}. If you intend to add a YAML path, ensure "
            f"orphan items are STILL included in the Pricempire "
            f"poll set (ADR 024)."
        )
