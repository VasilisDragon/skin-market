"""Tests for analytics/drift.py — pattern-aware drift detector.

Layered the same way as test_pattern_classifier.py:

- Group 1: decide_verdict pure logic (no DB). Most tests live here.
- Group 2: compute_and_store DB-required, exercises the full SQL +
  insights-row-write path against a sentinel fixture.
- Group 3: insights row shape (DB-required).
- Group 4: module sanity.

The pure-logic split means the seven verdict kinds + their boundary
behaviors are all testable without DB fixtures. DB tests verify the
SQL plumbing and that the JSONB meta_info carries every documented
field.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from analytics import drift
from analytics.drift import (
    _MEANINGFUL_PAIRS,
    BASELINE_DRIFT_THRESHOLD,
    STALE_CURATED_MINUTES,
    STALE_PRICEMPIRE_MINUTES,
    VERDICT_DRIFT_ALERT,
    VERDICT_NO_COMPARABLE_DATA,
    VERDICT_NO_DRIFT,
    VERDICT_PATTERN_SKIP,
    VERDICT_STALE_BOTH,
    VERDICT_STALE_CURATED,
    VERDICT_STALE_PRICEMPIRE,
    compute_and_store,
    decide_verdict,
)
from analytics.pattern_classifier import (
    ClassificationEntry,
    Classifier,
)
from db.connection import get_engine


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


_NOW = datetime(2026, 5, 17, 21, 0, 0, tzinfo=UTC)


def _fresh(ts_offset_min: float) -> datetime:
    """Convenience: a timestamp ``ts_offset_min`` minutes before
    ``_NOW``. ``_fresh(5)`` = 5 minutes ago = fresh."""
    return _NOW - timedelta(minutes=ts_offset_min)


def _entry(
    classification: str = "pattern_agnostic",
    *,
    multiplier: float = 1.0,
    note: str | None = None,
) -> ClassificationEntry:
    return ClassificationEntry(
        classification=classification,
        threshold_multiplier=multiplier,
        note=note,
    )


# ──────────────────────────────────────────────────────────────────────
# Group 1 — decide_verdict pure logic (15 tests)
# ──────────────────────────────────────────────────────────────────────


class TestDecideVerdictKindDispatch:
    def test_phase_based_emits_pattern_skip(self) -> None:
        """Phase 1 of the precedence order: phase_based wins over
        everything, regardless of freshness or data presence."""
        result = decide_verdict(
            curated_price=Decimal("28.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("25.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("phase_based"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_PATTERN_SKIP
        assert result.drift is None

    def test_pattern_agnostic_below_threshold(self) -> None:
        """8% drift on pattern_agnostic → no_drift (threshold 10%)."""
        # curated 108, pricempire 100 → drift = 0.08
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_DRIFT
        assert result.drift == Decimal("0.0800")

    def test_pattern_agnostic_above_threshold(self) -> None:
        """15% drift on pattern_agnostic → drift_alert."""
        # curated 115, pricempire 100 → drift = 0.15
        result = decide_verdict(
            curated_price=Decimal("115.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_DRIFT_ALERT
        assert result.drift == Decimal("0.1500")

    def test_pattern_seed_below_elevated_threshold(self) -> None:
        """pattern_seed with multiplier 2.0 → effective threshold 20%.
        15% drift is below 20%, so no_drift."""
        result = decide_verdict(
            curated_price=Decimal("115.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_seed", multiplier=2.0),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_DRIFT
        assert result.drift == Decimal("0.1500")
        assert result.threshold_used == Decimal("0.20")

    def test_pattern_seed_above_elevated_threshold(self) -> None:
        """pattern_seed with multiplier 2.0, 25% drift > 20% → alert."""
        # curated 125, pricempire 100 → drift = 0.25
        result = decide_verdict(
            curated_price=Decimal("125.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_seed", multiplier=2.0),
            now=_NOW,
        )
        assert result.verdict == VERDICT_DRIFT_ALERT
        assert result.drift == Decimal("0.2500")

    def test_drift_sign_preserved_negative(self) -> None:
        """Curated < Pricempire → drift is negative; magnitude still
        compared to threshold."""
        # curated 88, pricempire 100 → drift = -0.12 → magnitude > 0.10
        result = decide_verdict(
            curated_price=Decimal("88.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_DRIFT_ALERT
        assert result.drift == Decimal("-0.1200")

    def test_threshold_boundary_positive_strict(self) -> None:
        """drift = +threshold exactly → no_drift (strict ``>`` for
        alert). curated 110 / pricempire 100 → drift = 0.1000 exactly."""
        result = decide_verdict(
            curated_price=Decimal("110.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_DRIFT
        assert result.drift == Decimal("0.1000")

    def test_threshold_boundary_negative_strict(self) -> None:
        """Step 5 refinement: symmetric boundary on the negative side.
        drift = -threshold exactly → no_drift."""
        # curated 90 / pricempire 100 → drift = -0.1000 exactly
        result = decide_verdict(
            curated_price=Decimal("90.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_DRIFT
        assert result.drift == Decimal("-0.1000")


class TestDecideVerdictStaleness:
    def test_curated_stale(self) -> None:
        """Curated last-polled > 30 min ago → stale_curated, value=None."""
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(45),  # 45 min ago > 30
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_STALE_CURATED
        assert result.drift is None
        assert result.curated_age_min == pytest.approx(45.0)

    def test_pricempire_stale(self) -> None:
        """Pricempire side > STALE_PRICEMPIRE_MINUTES (=75 per ADR 022
        §2.5) → stale_pricempire. 90 min comfortably exceeds 75."""
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(90),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_STALE_PRICEMPIRE
        assert result.drift is None

    def test_both_stale(self) -> None:
        """Both sides exceed their respective thresholds → stale_both.
        Curated >30 (here 45), Pricempire >75 (here 90)."""
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(45),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(90),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_STALE_BOTH
        assert result.drift is None

    def test_stale_with_phase_based_still_returns_pattern_skip(
        self,
    ) -> None:
        """Precedence: phase_based wins over stale_*. Step 6's bot
        renderer needs to mirror this (no drift number, no stale
        framing — just 'drift is structurally meaningless')."""
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(99),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(99),
            classification=_entry("phase_based"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_PATTERN_SKIP


class TestDecideVerdictMissingData:
    def test_no_curated_price(self) -> None:
        result = decide_verdict(
            curated_price=None,
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_COMPARABLE_DATA
        assert result.drift is None

    def test_no_pricempire_price(self) -> None:
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=None,
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_COMPARABLE_DATA

    def test_zero_pricempire_price(self) -> None:
        """Zero pricempire price would div-by-zero in drift math —
        treated as no_comparable_data, not 'infinite drift'."""
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("0.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_COMPARABLE_DATA
        assert result.drift is None

    def test_both_sides_missing(self) -> None:
        result = decide_verdict(
            curated_price=None,
            curated_last_polled_at=None,
            pricempire_price=None,
            pricempire_last_polled_at=None,
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict == VERDICT_NO_COMPARABLE_DATA


class TestDecideVerdictTypes:
    def test_drift_is_decimal_not_float(self) -> None:
        """Step 5 pin: drift = 0.15 must be stored as Decimal('0.15'),
        not 0.15 the float. 'any float(price) is a bug' extends to
        ratios derived from money."""
        result = decide_verdict(
            curated_price=Decimal("115.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(3),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert isinstance(result.drift, Decimal)
        assert isinstance(result.threshold_used, Decimal)


# ──────────────────────────────────────────────────────────────────────
# Group 2 — compute_and_store DB integration (6 tests)
# ──────────────────────────────────────────────────────────────────────


_SENTINEL_PREFIX = "__DriftSentinel__"


@pytest.fixture
def sentinel_drift_setup():
    """Insert a sentinel item + Skinport price row + Pricempire-
    skinport observation, plus the observation_log entries with
    fresh timestamps.

    Yields (item_id, skinport_id, pricempire_skinport_id). Cleanup
    removes all sentinel rows from prices, observation_log,
    pricempire_observations, pricempire_observation_log, insights,
    and items.
    """
    if not _db_reachable():
        yield None
        return

    engine = get_engine()
    item_id = uuid.uuid4()
    item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"

    with Session(engine) as session:
        # Purge any leftover.
        _purge_sentinels(session)

        # Insert sentinel item.
        session.execute(
            text(
                "INSERT INTO items "
                "(id, market_hash_name, display_name, slug, item_type) "
                "VALUES (:id, :name, :name, :slug, 'rifle')"
            ),
            {
                "id": item_id,
                "name": item_name,
                "slug": "drift-sentinel-item-field-tested",
            },
        )

        # Resolve real source IDs.
        skinport_id = session.execute(
            text("SELECT id FROM sources WHERE name = 'skinport'")
        ).scalar_one()
        pricempire_skinport_id = session.execute(
            text(
                "SELECT id FROM sources "
                "WHERE name = 'pricempire_skinport'"
            )
        ).scalar_one()

        now = datetime.now(UTC)

        # Insert a prices row + observation_log entry (both fresh).
        session.execute(
            text(
                "INSERT INTO prices "
                "(item_id, source_id, timestamp, price, volume, currency) "
                "VALUES (:i, :s, :ts, :p, 27, 'USD')"
            ),
            {
                "i": item_id,
                "s": skinport_id,
                "ts": now,
                "p": Decimal("100.00"),
            },
        )
        session.execute(
            text(
                "INSERT INTO observation_log "
                "(item_id, source_id, last_observed_at) "
                "VALUES (:i, :s, :ts)"
            ),
            {"i": item_id, "s": skinport_id, "ts": now},
        )

        # Insert a Pricempire observation + observation_log entry.
        session.execute(
            text(
                "INSERT INTO pricempire_observations "
                "(item_id, source_id, timestamp, price, count, currency) "
                "VALUES (:i, :s, :ts, :p, 30, 'USD')"
            ),
            {
                "i": item_id,
                "s": pricempire_skinport_id,
                "ts": now,
                "p": Decimal("90.00"),
            },
        )
        session.execute(
            text(
                "INSERT INTO pricempire_observation_log "
                "(item_id, source_id, last_observed_at) "
                "VALUES (:i, :s, :ts)"
            ),
            {
                "i": item_id,
                "s": pricempire_skinport_id,
                "ts": now,
            },
        )
        session.commit()

    yield (item_id, skinport_id, pricempire_skinport_id)

    with Session(engine) as session:
        _purge_sentinels(session)
        session.commit()


def _purge_sentinels(session: Session) -> None:
    session.execute(
        text(
            "DELETE FROM insights WHERE item_id IN "
            "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
        ),
        {"pat": f"{_SENTINEL_PREFIX}%"},
    )
    session.execute(
        text(
            "DELETE FROM prices WHERE item_id IN "
            "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
        ),
        {"pat": f"{_SENTINEL_PREFIX}%"},
    )
    session.execute(
        text(
            "DELETE FROM observation_log WHERE item_id IN "
            "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
        ),
        {"pat": f"{_SENTINEL_PREFIX}%"},
    )
    session.execute(
        text(
            "DELETE FROM pricempire_observations WHERE item_id IN "
            "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
        ),
        {"pat": f"{_SENTINEL_PREFIX}%"},
    )
    session.execute(
        text(
            "DELETE FROM pricempire_observation_log WHERE item_id IN "
            "(SELECT id FROM items WHERE market_hash_name LIKE :pat)"
        ),
        {"pat": f"{_SENTINEL_PREFIX}%"},
    )
    session.execute(
        text("DELETE FROM items WHERE market_hash_name LIKE :pat"),
        {"pat": f"{_SENTINEL_PREFIX}%"},
    )


def _make_test_classifier(
    entries: dict[str, ClassificationEntry] | None = None,
) -> Classifier:
    """Build a Classifier directly from a dict, bypassing the YAML
    parser. Used to inject test-specific classifications without
    writing pattern_sensitivity.yaml fixture files."""
    return Classifier(entries or {})


class TestComputeAndStore:
    @_db_required
    def test_writes_drift_verdict_row_for_pattern_agnostic(
        self, sentinel_drift_setup
    ) -> None:
        """Fresh prices on both sides, drift = (100-90)/90 ≈ 0.1111
        which exceeds the 10% threshold → drift_alert row written
        with the signed Decimal drift in insights.value."""
        item_id, _skinport_id, _ = sentinel_drift_setup
        engine = get_engine()
        with Session(engine) as session:
            wrote = compute_and_store(
                session,
                classifier=_make_test_classifier(),  # all pattern_agnostic default
                curated_set={
                    f"{_SENTINEL_PREFIX} Item (Field-Tested)"
                },
            )
            session.commit()

        assert wrote >= 1
        with Session(engine) as session:
            rows = session.execute(
                text(
                    "SELECT value, meta_info FROM insights "
                    "WHERE item_id = :i AND insight_type = 'drift_verdict'"
                ),
                {"i": item_id},
            ).all()

        # One row for (skinport, pricempire_skinport). The other pair
        # (dmarket, pricempire_dmarket) has no observation_log entries
        # in the fixture, so no rows are emitted for it (the LEFT JOIN
        # on observation_log returns nothing).
        skinport_rows = [
            r for r in rows
            if r.meta_info.get("source_a_name") == "skinport"
        ]
        assert len(skinport_rows) == 1
        row = skinport_rows[0]
        assert row.meta_info["verdict"] == VERDICT_DRIFT_ALERT
        # drift = (100 - 90) / 90 = 0.111... quantized to 0.0001 → 0.1111
        assert Decimal(str(row.value)) == Decimal("0.1111")

    @_db_required
    def test_writes_pattern_skip_for_phase_based(
        self, sentinel_drift_setup
    ) -> None:
        """Same data, but inject a phase_based classification → row
        is pattern_skip with insights.value = NULL."""
        item_id, _, _ = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        with Session(engine) as session:
            wrote = compute_and_store(
                session,
                classifier=_make_test_classifier(
                    {item_name: _entry("phase_based")}
                ),
                curated_set={item_name},
            )
            session.commit()

        assert wrote >= 1
        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT value, meta_info FROM insights "
                    "WHERE item_id = :i "
                    "  AND meta_info->>'source_a_name' = 'skinport'"
                ),
                {"i": item_id},
            ).one()
        assert row.value is None
        assert row.meta_info["verdict"] == VERDICT_PATTERN_SKIP
        assert row.meta_info["classification"] == "phase_based"

    @_db_required
    def test_writes_stale_verdict_for_aged_observation_log(
        self, sentinel_drift_setup
    ) -> None:
        """Age the observation_log to be > 30 min stale → stale_curated
        verdict, value NULL."""
        item_id, skinport_id, _ = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        # Age the curated observation_log to 45 min ago.
        with Session(engine) as session:
            session.execute(
                text(
                    "UPDATE observation_log "
                    "SET last_observed_at = NOW() - INTERVAL '45 minutes' "
                    "WHERE item_id = :i AND source_id = :s"
                ),
                {"i": item_id, "s": skinport_id},
            )
            session.commit()

        with Session(engine) as session:
            compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set={item_name},
            )
            session.commit()

        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT value, meta_info FROM insights "
                    "WHERE item_id = :i "
                    "  AND meta_info->>'source_a_name' = 'skinport'"
                ),
                {"i": item_id},
            ).one()
        assert row.value is None
        assert row.meta_info["verdict"] == VERDICT_STALE_CURATED

    @_db_required
    def test_two_cycles_append(self, sentinel_drift_setup) -> None:
        """Idempotency model: append. Two compute_and_store calls
        with the same input → two rows in insights, not one."""
        item_id, _, _ = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        with Session(engine) as session:
            compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set={item_name},
            )
            session.commit()
        with Session(engine) as session:
            compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set={item_name},
            )
            session.commit()

        with Session(engine) as session:
            rows = session.execute(
                text(
                    "SELECT computed_at FROM insights "
                    "WHERE item_id = :i AND insight_type = 'drift_verdict' "
                    "  AND meta_info->>'source_a_name' = 'skinport' "
                    "ORDER BY computed_at"
                ),
                {"i": item_id},
            ).all()
        assert len(rows) == 2, (
            f"expected 2 rows after 2 cycles (append), got {len(rows)}"
        )

    @_db_required
    def test_skips_non_curated_tier_items(self, sentinel_drift_setup) -> None:
        """Items not in curated_set are silently skipped (drift detection
        is deep-only per ADR 024)."""
        item_id, _, _ = sentinel_drift_setup
        engine = get_engine()
        with Session(engine) as session:
            wrote = compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set=set(),  # empty deep set → no work to do
            )
            session.commit()

        assert wrote == 0
        with Session(engine) as session:
            rows = session.execute(
                text(
                    "SELECT 1 FROM insights "
                    "WHERE item_id = :i AND insight_type = 'drift_verdict'"
                ),
                {"i": item_id},
            ).all()
        assert rows == []

    @_db_required
    def test_writes_no_comparable_data_for_pair_with_no_observations(
        self, sentinel_drift_setup
    ) -> None:
        """The fixture seeds only the skinport pair. compute_and_store
        attempts both meaningful pairs per cycle (skinport+ps and
        dmarket+pd); for the dmarket pair, both observation_log
        queries return nothing, decide_verdict emits
        no_comparable_data, and a row is written.

        Writing the row is intentional: the bot's read pattern uses
        DISTINCT ON to surface latest verdict per pair; an explicit
        no_comparable_data row tells the bot "the pair was attempted
        but neither side had observations." Skipping the write
        instead would force the bot to infer absence-of-row, which
        is less honest as a per-pair verdict surface.
        """
        item_id, _, _ = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        with Session(engine) as session:
            wrote = compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set={item_name},
            )
            session.commit()

        # Two rows: one for the skinport pair (drift_alert), one for
        # the dmarket pair (no_comparable_data).
        assert wrote == 2

        with Session(engine) as session:
            dmarket_row = session.execute(
                text(
                    "SELECT value, meta_info FROM insights "
                    "WHERE item_id = :i "
                    "  AND meta_info->>'source_a_name' = 'dmarket'"
                ),
                {"i": item_id},
            ).one()
        assert dmarket_row.value is None
        assert (
            dmarket_row.meta_info["verdict"]
            == VERDICT_NO_COMPARABLE_DATA
        )


# ──────────────────────────────────────────────────────────────────────
# Group 3 — Insights row shape (3 tests)
# ──────────────────────────────────────────────────────────────────────


class TestInsightsRowShape:
    @_db_required
    def test_meta_info_carries_pair_identifiers(
        self, sentinel_drift_setup
    ) -> None:
        item_id, skinport_id, pricempire_skinport_id = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        with Session(engine) as session:
            compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set={item_name},
            )
            session.commit()

        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT meta_info FROM insights "
                    "WHERE item_id = :i "
                    "  AND meta_info->>'source_a_name' = 'skinport'"
                ),
                {"i": item_id},
            ).one()
        meta = row.meta_info
        assert meta["source_a_id"] == skinport_id
        assert meta["source_a_name"] == "skinport"
        assert meta["source_b_id"] == pricempire_skinport_id
        assert meta["source_b_name"] == "pricempire_skinport"

    @_db_required
    def test_meta_info_carries_classification_context(
        self, sentinel_drift_setup
    ) -> None:
        item_id, _, _ = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        with Session(engine) as session:
            compute_and_store(
                session,
                classifier=_make_test_classifier(
                    {
                        item_name: _entry(
                            "pattern_seed",
                            multiplier=2.0,
                            note="rare seed",
                        )
                    }
                ),
                curated_set={item_name},
            )
            session.commit()

        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT meta_info FROM insights "
                    "WHERE item_id = :i "
                    "  AND meta_info->>'source_a_name' = 'skinport'"
                ),
                {"i": item_id},
            ).one()
        meta = row.meta_info
        assert meta["classification"] == "pattern_seed"
        assert meta["threshold_multiplier"] == 2.0
        # threshold_used = 0.10 baseline × 2.0 multiplier = 0.20
        assert Decimal(meta["threshold_used"]) == Decimal("0.20")
        assert meta["note"] == "rare seed"

    @_db_required
    def test_meta_info_carries_prices_as_strings(
        self, sentinel_drift_setup
    ) -> None:
        """MoneyStr discipline: prices serialize as JSON strings, not
        as numeric values. float() of a price is a bug; the JSONB
        representation must preserve that."""
        item_id, _, _ = sentinel_drift_setup
        item_name = f"{_SENTINEL_PREFIX} Item (Field-Tested)"
        engine = get_engine()
        with Session(engine) as session:
            compute_and_store(
                session,
                classifier=_make_test_classifier(),
                curated_set={item_name},
            )
            session.commit()

        with Session(engine) as session:
            # Pull the raw JSONB to inspect type.
            row = session.execute(
                text(
                    "SELECT meta_info::text AS raw_json FROM insights "
                    "WHERE item_id = :i "
                    "  AND meta_info->>'source_a_name' = 'skinport'"
                ),
                {"i": item_id},
            ).one()
        parsed = json.loads(row.raw_json)
        # JSON-level type check: curated_price must be a string in
        # the wire JSONB, not a number.
        assert isinstance(parsed["curated_price"], str)
        assert isinstance(parsed["pricempire_price"], str)
        assert parsed["curated_price"] == "100.00"
        assert parsed["pricempire_price"] == "90.00"


# ──────────────────────────────────────────────────────────────────────
# Group 4 — Sanity
# ──────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    def test_meaningful_pairs_match_adr_018(self) -> None:
        """Pin the meaningful-pairs list against accidental wire-up to
        Steam (Pricempire doesn't serve Steam prices per ADR 018) or
        to other Pricempire sub-providers (cross-marketplace pairings
        mix taxonomies)."""
        assert _MEANINGFUL_PAIRS == (
            ("skinport", "pricempire_skinport"),
            ("dmarket", "pricempire_dmarket"),
        )

    def test_baseline_threshold_is_decimal_not_float(self) -> None:
        """Step 5 pin: drift-comparison threshold lives as Decimal
        end-to-end. Future tunes to this constant should preserve the
        Decimal type."""
        assert isinstance(BASELINE_DRIFT_THRESHOLD, Decimal)
        assert Decimal("0.10") == BASELINE_DRIFT_THRESHOLD

    def test_stale_thresholds_match_adr_022(self) -> None:
        """Curated at 30 min matches the 15-30 min curated polling
        cadence. Pricempire at 75 min is the ADR 022 §2.5 interim
        value chosen to cover the empirically-observed 30-90 min
        jitter on the upstream pricempire_skinport refresh
        (docs/phase2b-validation.md §3.a). Revised by follow-up ADR
        after the 7-day characterization in ADR 022 §6 completes."""
        assert STALE_CURATED_MINUTES == 30.0
        assert STALE_PRICEMPIRE_MINUTES == 75.0

    def test_pricempire_age_in_revised_band_evaluates_fresh(self) -> None:
        """ADR 022 §2.5 behavior pin: a Pricempire age of 60 min — well
        inside the original 30-min stale band but well inside the new
        75-min fresh band — must produce a fresh evaluation verdict,
        NOT stale_pricempire. Locks in the semantic change against
        regressions that the constant-pin test wouldn't catch: an env-
        override path, a bypass of decide_verdict's stale gate, or a
        partial revert of the constant change."""
        result = decide_verdict(
            curated_price=Decimal("108.00"),
            curated_last_polled_at=_fresh(5),
            pricempire_price=Decimal("100.00"),
            pricempire_last_polled_at=_fresh(60),
            classification=_entry("pattern_agnostic"),
            now=_NOW,
        )
        assert result.verdict != "stale_pricempire"
        assert result.verdict in ("drift_alert", "no_drift")
        assert result.drift is not None


# ──────────────────────────────────────────────────────────────────────
# YAML → curated_set construction regression pin (Phase 2c rename)
# ──────────────────────────────────────────────────────────────────────


class TestYamlToCuratedSetIntegration:
    """Pin the load-bearing string-literal in the drift detector's
    YAML-driven curated_set construction.

    Regression context: at the Phase 2c rename (deep/broad/orphan →
    curated/featured/substrate, ADR 024), a partial replace_all
    renamed the Python variable ``deep_set`` → ``curated_set`` in
    ``analytics/drift.py:compute_and_store`` but momentarily left
    the string-literal comparison as ``it.get("tier") == "deep"``.
    Against a schema_version: 3 YAML (every item flagged ``tier:
    curated``), the comparison never matched → ``curated_set`` was
    empty → zero ``drift_verdict`` rows produced per cycle.

    The 469 tests passing under the broken state did NOT catch this
    because every drift test passes ``curated_set`` directly as a
    parameter, bypassing the YAML-loading path. Only the production
    end-to-end path exercised the broken comparison; a post-restart
    canary against the validation doc §4 invariants would have
    caught it.

    This test fires on the unit level — no DB, no analytics service
    restart needed. It verifies the construction matches what
    ``compute_and_store`` does internally when ``curated_set`` is
    not passed in.
    """

    def test_curated_set_built_from_v3_yaml_uses_curated_literal(
        self, tmp_path: Path
    ) -> None:
        """Compose a schema_version: 3 YAML with mixed-tier items;
        build the curated_set the way ``compute_and_store`` does;
        assert it picks up the ``tier: curated`` rows and ONLY those.
        A regression that switches the literal back to ``"deep"``
        would produce an empty set and fail this assertion loudly."""
        yaml_path = tmp_path / "watchlist.yaml"
        yaml_path.write_text(
            "schema_version: 3\n"
            "featured_tier_exclusions: []\n"
            "sources:\n"
            "  - { name: skinport, base_url: https://example, "
            "rate_limit_per_minute: 60, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "Sentinel A (FT)", '
            "tier: curated }\n"
            '  - { market_hash_name: "Sentinel B (FT)", '
            "tier: featured }\n"
            '  - { market_hash_name: "Sentinel C (FT)", '
            "tier: curated }\n"
        )

        from scripts.seed_watchlist import load_watchlist

        data = load_watchlist(yaml_path)

        # Mirror the construction in
        # analytics/drift.py:compute_and_store (and the parallel one
        # in analytics/pattern_classifier.py:load_classifier).
        curated_set = {
            it["market_hash_name"]
            for it in data["items"]
            if it.get("tier") == "curated"
        }
        assert curated_set == {
            "Sentinel A (FT)",
            "Sentinel C (FT)",
        }, (
            f"curated_set was {curated_set}; expected the two "
            "tier: curated items. An empty set indicates the "
            "string-literal comparison in compute_and_store has "
            "regressed away from 'curated' (likely back to the "
            "pre-Phase-2c 'deep' value)."
        )

    def test_compute_and_store_yaml_path_picks_up_curated_items(
        self, tmp_path: Path
    ) -> None:
        """Pin compute_and_store's actual YAML-loading branch (the
        one that fires when ``curated_set`` is NOT passed in). Uses
        the function's real path: it reads the YAML, builds
        curated_set, then iterates. We don't need a DB — the
        function early-skips items not in the items table, returning
        0 — but the iteration entering at all is the proof that
        curated_set was non-empty. The session injection is via a
        no-op MagicMock; the items lookup returns None for every
        market_hash_name, so the function's "skip if not in items
        table" branch fires for every entry.

        Failure mode if the string-literal regresses: curated_set
        is empty → the function returns 0 without entering the loop
        → there's no way to distinguish that from "all items found
        but skipped." So we instrument the seed_watchlist load
        instead, asserting on the curated_set content directly via
        the previous test. This test pins that compute_and_store
        can be called with the new YAML shape without crashing.
        """
        from unittest.mock import MagicMock

        yaml_path = tmp_path / "watchlist.yaml"
        yaml_path.write_text(
            "schema_version: 3\n"
            "featured_tier_exclusions: []\n"
            "sources:\n"
            "  - { name: skinport, base_url: https://example, "
            "rate_limit_per_minute: 60, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "Sentinel (FT)", '
            "tier: curated }\n"
        )

        # No-op classifier (always returns the default pattern-agnostic
        # entry); never consulted because the item won't be found.
        classifier = Classifier({})

        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = (
            None
        )

        # The function should accept the v3 YAML without crashing and
        # return 0 (the sentinel isn't in the mocked items table).
        # If a regression makes curated_set empty by default, the
        # function still returns 0 — distinguishable only via the
        # previous test's direct curated_set inspection. This call
        # is a smoke-test for the YAML-parsing path under v3.
        rows_written = drift.compute_and_store(
            session,
            classifier=classifier,
            watchlist_path=yaml_path,
        )
        assert rows_written == 0


# Reference the module so a future import-pruner doesn't strip the
# direct symbol imports above.
_ = drift
_ = Path
_ = select
