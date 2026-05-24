"""Pattern-aware drift detector.

Compares each curated-tier item's direct-collector price against the
corresponding Pricempire sub-provider price. Emits a `drift_verdict`
insights row per (item, meaningful-pair) per cycle. Uses the
pattern-sensitivity classifier (ADR 021) to skip phase-bearing items
outright and to elevate the drift threshold for pattern-seed items.

Meaningful pairs per ADR 018 (Pricempire doesn't serve Steam; cross-
marketplace pairings mix taxonomies):

    (skinport, pricempire_skinport)
    (dmarket,  pricempire_dmarket)

Two pairs per curated-tier item are evaluated per cycle.

Verdict kinds (seven; ``insights.value`` is set only on the first two):

    drift_alert          |effective_threshold < |drift|              → value = signed Decimal
    no_drift             |drift| ≤ effective_threshold               → value = signed Decimal
    pattern_skip         classifier says phase_based                 → value = NULL
    stale_curated        curated last-polled > STALE_CURATED_MINUTES → value = NULL
    stale_pricempire     pricempire last-observed > STALE_PRICEMPIRE → value = NULL
    stale_both           both sides stale                            → value = NULL
    no_comparable_data   one side missing or pricempire_price = 0    → value = NULL

Boundary: ``abs(drift) > effective_threshold`` triggers drift_alert.
The threshold itself sits on the no_drift side (drift = ±threshold
exactly emits no_drift). Tests pin both signs.

Freshness contract: 30-minute stale threshold on both sides matches
the detector's 30-minute cadence (one full Pricempire cycle + at
least 0.5 curated cycles between detector runs). Both sides keyed
off their respective observation_log table (the pre-dedup write
ensures freshness is independent from price-row deduplication).

Idempotency: append. Each cycle writes one row per (item, pair); the
read pattern is ``DISTINCT ON (item_id, source_a_id, source_b_id)
ORDER BY computed_at DESC`` (mirrors the existing /items/{slug}/insights
shape). Re-running compute_and_store within the same window is safe
but adds duplicate rows; downstream readers see only the latest.

Loading discipline: ``compute_and_store`` loads the classifier YAML
and the watchlist YAML ONCE at cycle start, not per-item. Both are
treated as immutable for the duration of the cycle. The
curated-tier filter is read from the watchlist YAML directly (ADR 024:
tier lives in YAML, not the items table).

Insights-table coexistence with ``cross_source_divergence``:
disjoint by construction. ``cross_source_divergence`` is computed
from ``cross_source_spread`` rows, which are emitted only for source
pairs whose latest prices come from the ``prices`` table — i.e. both
sides are direct collectors. ``drift_verdict`` always pairs a direct
collector with a Pricempire sub-provider. The meta_signature shapes
never collide; both insight types coexist in the insights table
without interaction. Bot rendering gives drift verdicts precedence for
any pair involving a Pricempire source.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from analytics.pattern_classifier import (
    DEFAULT_PATTERN_PATH,
    ClassificationEntry,
    Classifier,
    load_classifier,
)
from scripts.seed_watchlist import (
    DEFAULT_WATCHLIST_PATH,
    load_watchlist,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

# 10% catches material divergence while staying clear of normal
# inter-source noise for pattern-agnostic items.
BASELINE_DRIFT_THRESHOLD: Decimal = Decimal("0.10")

# Stale thresholds. Curated at 30 min matches the curated polling
# cadence (15-30 min); the gate fires only on actual misses.
#
# Pricempire at 75 min is an INTERIM value per ADR 022 §2.5. The
# original 30-min constant was empirically mis-calibrated: Pricempire's
# upstream pricempire_skinport refresh runs ~60 min mean with ±30 min
# jitter (docs/phase2b-validation.md §3.a), producing structural every-
# other-cycle false-positive `stale_pricempire` verdicts. 75 covers
# the 90-min worst-case jitter observed in the 21h validation window
# while staying tight enough that a real multi-hour Pricempire outage
# still trips the gate. Revised by follow-up ADR after the 7-day
# characterization in ADR 022 §6 completes.
#
# Module constants; no env override (operational changes go through
# an ADR amendment + code change, not an env variable).
STALE_CURATED_MINUTES: float = 30.0
STALE_PRICEMPIRE_MINUTES: float = 75.0

# Meaningful source pairs per ADR 018. Tuple of (curated_name,
# pricempire_sub_provider_name). Test
# `test_meaningful_pairs_match_adr_018` pins this against accidental
# wire-up to Steam or to other Pricempire sub-providers (e.g.
# pricempire_buff163, which would mix taxonomies).
_MEANINGFUL_PAIRS: tuple[tuple[str, str], ...] = (
    ("skinport", "pricempire_skinport"),
    ("dmarket", "pricempire_dmarket"),
)

# Verdict kinds imported by the API and bot layers.
VERDICT_DRIFT_ALERT = "drift_alert"
VERDICT_NO_DRIFT = "no_drift"
VERDICT_PATTERN_SKIP = "pattern_skip"
VERDICT_STALE_CURATED = "stale_curated"
VERDICT_STALE_PRICEMPIRE = "stale_pricempire"
VERDICT_STALE_BOTH = "stale_both"
VERDICT_NO_COMPARABLE_DATA = "no_comparable_data"

# Drift precision: 4 decimal places (0.01% resolution). Quantized at
# computation time so stored values + JSONB strings are stable across
# runs. Avoids cluttering insights.value with arbitrary trailing
# digits from Decimal division precision.
_DRIFT_QUANTUM: Decimal = Decimal("0.0001")


# ──────────────────────────────────────────────────────────────────────
# Public data shape
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerdictResult:
    """Output of ``decide_verdict``. All fields the persister needs to
    build the insights row + meta_info JSONB. Frozen for safety —
    callers must not mutate."""

    verdict: str  # one of VERDICT_* constants
    drift: Decimal | None  # signed ratio; None for non-numeric verdicts
    threshold_used: Decimal  # effective threshold = baseline * multiplier
    classification: str  # mirrored from ClassificationEntry
    threshold_multiplier: float  # mirrored from ClassificationEntry
    note: str | None  # operator-facing rationale (optional)
    curated_price: Decimal | None
    pricempire_price: Decimal | None
    curated_age_min: float | None  # minutes; None if no observation_log row
    pricempire_age_min: float | None


# ──────────────────────────────────────────────────────────────────────
# Layer 1 — decide_verdict (pure logic, no DB)
# ──────────────────────────────────────────────────────────────────────


def decide_verdict(
    *,
    curated_price: Decimal | None,
    curated_last_polled_at: datetime | None,
    pricempire_price: Decimal | None,
    pricempire_last_polled_at: datetime | None,
    classification: ClassificationEntry,
    now: datetime,
    baseline_threshold: Decimal = BASELINE_DRIFT_THRESHOLD,
    stale_curated_minutes: float = STALE_CURATED_MINUTES,
    stale_pricempire_minutes: float = STALE_PRICEMPIRE_MINUTES,
) -> VerdictResult:
    """Pure decision function. No DB or filesystem access.

    Precedence order (the first match wins):

    1. ``classification.classification == "phase_based"`` → pattern_skip.
       The skip is structural, not freshness-dependent. Bot rendering
       mirrors this precedence with no drift number and no stale framing.

    2. Missing data: ``curated_price`` or ``pricempire_price`` is
       None, OR ``pricempire_price == 0`` (would div-by-zero in
       drift math) → no_comparable_data.

    3. Both ages exceed their stale thresholds → stale_both.

    4. One age exceeds its stale threshold → stale_curated or
       stale_pricempire.

    5. Both fresh: compute drift as signed
       ``(curated - pricempire) / pricempire``, quantized to 4
       decimal places. Compare ``abs(drift)`` to effective threshold
       ``baseline × multiplier``; ``> threshold`` triggers
       drift_alert, ``≤ threshold`` triggers no_drift.

    The boundary ``drift = ±threshold exactly`` lands on the no_drift
    side (the ``>`` strict-greater check). Symmetric on both signs.
    """
    # Threshold computed once, as Decimal, for the comparison and for
    # the VerdictResult.threshold_used field. Money-derived numbers
    # stay Decimal end-to-end ("any float(price) is a bug" extends to
    # ratios derived from money).
    effective_threshold = (
        baseline_threshold
        * Decimal(str(classification.threshold_multiplier))
    )

    curated_age_min = _age_minutes(curated_last_polled_at, now)
    pricempire_age_min = _age_minutes(pricempire_last_polled_at, now)

    # 1. phase_based wins over everything else.
    if classification.classification == "phase_based":
        return VerdictResult(
            verdict=VERDICT_PATTERN_SKIP,
            drift=None,
            threshold_used=effective_threshold,
            classification=classification.classification,
            threshold_multiplier=classification.threshold_multiplier,
            note=classification.note,
            curated_price=curated_price,
            pricempire_price=pricempire_price,
            curated_age_min=curated_age_min,
            pricempire_age_min=pricempire_age_min,
        )

    # 2. Missing data on either side (or zero pricempire price).
    if (
        curated_price is None
        or pricempire_price is None
        or pricempire_price == 0
    ):
        return VerdictResult(
            verdict=VERDICT_NO_COMPARABLE_DATA,
            drift=None,
            threshold_used=effective_threshold,
            classification=classification.classification,
            threshold_multiplier=classification.threshold_multiplier,
            note=classification.note,
            curated_price=curated_price,
            pricempire_price=pricempire_price,
            curated_age_min=curated_age_min,
            pricempire_age_min=pricempire_age_min,
        )

    # 3 & 4. Stale handling.
    curated_stale = (
        curated_age_min is not None
        and curated_age_min > stale_curated_minutes
    )
    pricempire_stale = (
        pricempire_age_min is not None
        and pricempire_age_min > stale_pricempire_minutes
    )

    if curated_stale and pricempire_stale:
        return _build_no_number_verdict(
            verdict=VERDICT_STALE_BOTH,
            effective_threshold=effective_threshold,
            classification=classification,
            curated_price=curated_price,
            pricempire_price=pricempire_price,
            curated_age_min=curated_age_min,
            pricempire_age_min=pricempire_age_min,
        )
    if curated_stale:
        return _build_no_number_verdict(
            verdict=VERDICT_STALE_CURATED,
            effective_threshold=effective_threshold,
            classification=classification,
            curated_price=curated_price,
            pricempire_price=pricempire_price,
            curated_age_min=curated_age_min,
            pricempire_age_min=pricempire_age_min,
        )
    if pricempire_stale:
        return _build_no_number_verdict(
            verdict=VERDICT_STALE_PRICEMPIRE,
            effective_threshold=effective_threshold,
            classification=classification,
            curated_price=curated_price,
            pricempire_price=pricempire_price,
            curated_age_min=curated_age_min,
            pricempire_age_min=pricempire_age_min,
        )

    # 5. Both fresh: compute drift and compare to threshold.
    drift = (
        (curated_price - pricempire_price) / pricempire_price
    ).quantize(_DRIFT_QUANTUM)
    verdict = (
        VERDICT_DRIFT_ALERT
        if abs(drift) > effective_threshold
        else VERDICT_NO_DRIFT
    )
    return VerdictResult(
        verdict=verdict,
        drift=drift,
        threshold_used=effective_threshold,
        classification=classification.classification,
        threshold_multiplier=classification.threshold_multiplier,
        note=classification.note,
        curated_price=curated_price,
        pricempire_price=pricempire_price,
        curated_age_min=curated_age_min,
        pricempire_age_min=pricempire_age_min,
    )


def _age_minutes(ts: datetime | None, now: datetime) -> float | None:
    """Minutes between ``ts`` and ``now``, or None when ``ts`` is None.

    Returns a non-negative float; clock skew producing ``ts > now``
    is clamped at 0.0 (treating "future timestamp" as "polled right
    now" rather than as a negative age).
    """
    if ts is None:
        return None
    delta = (now - ts).total_seconds() / 60.0
    return max(0.0, delta)


def _build_no_number_verdict(
    *,
    verdict: str,
    effective_threshold: Decimal,
    classification: ClassificationEntry,
    curated_price: Decimal | None,
    pricempire_price: Decimal | None,
    curated_age_min: float | None,
    pricempire_age_min: float | None,
) -> VerdictResult:
    """Build a VerdictResult for the four no-number verdict kinds
    (pattern_skip / stale_* / no_comparable_data). Centralizes the
    field-fill so decide_verdict's branches stay readable."""
    return VerdictResult(
        verdict=verdict,
        drift=None,
        threshold_used=effective_threshold,
        classification=classification.classification,
        threshold_multiplier=classification.threshold_multiplier,
        note=classification.note,
        curated_price=curated_price,
        pricempire_price=pricempire_price,
        curated_age_min=curated_age_min,
        pricempire_age_min=pricempire_age_min,
    )


# ──────────────────────────────────────────────────────────────────────
# Layer 2 — DB-bound orchestrator
# ──────────────────────────────────────────────────────────────────────


def compute_and_store(
    session: Session,
    *,
    now: datetime | None = None,
    classifier: Classifier | None = None,
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
    pattern_path: Path = DEFAULT_PATTERN_PATH,
    curated_set: set[str] | None = None,
) -> int:
    """Run one drift-detection cycle. Returns rows written.

    Reads the curated-tier set from watchlist.yaml (per ADR 024 — tier
    lives in YAML, not the items table). Loads the classifier once
    at cycle start. Both YAML files are treated as IMMUTABLE for the
    duration of the cycle. Per-item re-reads would invite inconsistent
    inputs and needless I/O.

    For each curated-tier item × each meaningful pair, queries the
    latest curated + Pricempire observations + their freshness
    timestamps, calls ``decide_verdict``, and INSERTs an
    ``insight_type = 'drift_verdict'`` row into the insights table.

    Append-only: each cycle writes one row per evaluated pair. The
    bot/API read pattern uses DISTINCT ON to surface the latest.

    ``classifier`` / ``curated_set`` parameters exist for test injection;
    production callers omit both and the function loads from disk.
    """
    now = now or datetime.now(UTC)

    # Load classifier ONCE at cycle start. Immutable for the cycle.
    if classifier is None:
        classifier = load_classifier(
            pattern_path,
            watchlist_path=watchlist_path,
            session=session,
        )

    # Load curated-tier set ONCE at cycle start. Immutable for the cycle.
    if curated_set is None:
        watchlist_data = load_watchlist(watchlist_path)
        curated_set = {
            it["market_hash_name"]
            for it in watchlist_data["items"]
            if it.get("tier") == "curated"
        }

    # Resolve source-name → source_id ONCE.
    source_id_by_name = _load_source_index(session)

    rows_written = 0

    for market_hash_name in sorted(curated_set):
        item_id = session.execute(
            text(
                "SELECT id FROM items "
                "WHERE market_hash_name = :name"
            ),
            {"name": market_hash_name},
        ).scalar_one_or_none()
        if item_id is None:
            # YAML drift: curated-tier item not in items table. Skip
            # silently; the upstream watchlist loader would have
            # caught this at deploy time.
            continue

        classification_entry = classifier.get(market_hash_name)

        for curated_name, pricempire_name in _MEANINGFUL_PAIRS:
            curated_id = source_id_by_name.get(curated_name)
            pricempire_id = source_id_by_name.get(pricempire_name)
            if curated_id is None or pricempire_id is None:
                # Source not in DB — skip silently; migrations
                # 0005/0006/seed_watchlist should have populated
                # these. Belt-and-braces.
                continue

            curated_price, curated_last_polled_at = (
                _fetch_curated_latest(session, item_id, curated_id)
            )
            pricempire_price, pricempire_last_polled_at = (
                _fetch_pricempire_latest(
                    session, item_id, pricempire_id
                )
            )

            result = decide_verdict(
                curated_price=curated_price,
                curated_last_polled_at=curated_last_polled_at,
                pricempire_price=pricempire_price,
                pricempire_last_polled_at=pricempire_last_polled_at,
                classification=classification_entry,
                now=now,
            )

            meta_info = _build_meta_info(
                result=result,
                source_a_id=curated_id,
                source_a_name=curated_name,
                source_b_id=pricempire_id,
                source_b_name=pricempire_name,
                curated_last_polled_at=curated_last_polled_at,
                pricempire_last_polled_at=pricempire_last_polled_at,
            )

            session.execute(
                text(
                    """
                    INSERT INTO insights
                        (item_id, computed_at, insight_type, value, meta_info)
                    VALUES (
                        :item_id, :now, 'drift_verdict', :value,
                        CAST(:meta AS jsonb)
                    )
                    """
                ),
                {
                    "item_id": item_id,
                    "now": now,
                    "value": result.drift,  # Decimal | None
                    "meta": json.dumps(meta_info),
                },
            )
            rows_written += 1

    return rows_written


def _load_source_index(session: Session) -> dict[str, int]:
    """Map source name → source_id for the lookups in compute_and_store."""
    rows = session.execute(text("SELECT name, id FROM sources")).all()
    return {r.name: r.id for r in rows}


def _fetch_curated_latest(
    session: Session, item_id, source_id: int
) -> tuple[Decimal | None, datetime | None]:
    """Return ``(price, last_polled_at)`` for a curated (item, source).

    Drives off ``observation_log`` for the freshness signal (the pre-
    dedup write — ADR 017). LEFT JOIN LATERAL preserves the
    observation_log row even when no prices row exists; in that case
    the returned price is None and decide_verdict emits
    no_comparable_data.
    """
    row = session.execute(
        text(
            """
            SELECT p.price, ol.last_observed_at
            FROM observation_log ol
            LEFT JOIN LATERAL (
                SELECT price FROM prices
                WHERE item_id = ol.item_id
                  AND source_id = ol.source_id
                ORDER BY timestamp DESC
                LIMIT 1
            ) p ON TRUE
            WHERE ol.item_id = :item_id
              AND ol.source_id = :source_id
            """
        ),
        {"item_id": item_id, "source_id": source_id},
    ).first()
    if row is None:
        return None, None
    return row.price, row.last_observed_at


def _fetch_pricempire_latest(
    session: Session, item_id, source_id: int
) -> tuple[Decimal | None, datetime | None]:
    """Return ``(price, last_observed_at)`` for a Pricempire
    (item, sub-provider).

    Drives off ``pricempire_observation_log`` for the freshness signal
    (ADR 023 — Pricempire's analog of ADR 017's observation_log).
    Same LEFT JOIN LATERAL pattern as the curated path.
    """
    row = session.execute(
        text(
            """
            SELECT p.price, pol.last_observed_at
            FROM pricempire_observation_log pol
            LEFT JOIN LATERAL (
                SELECT price FROM pricempire_observations
                WHERE item_id = pol.item_id
                  AND source_id = pol.source_id
                ORDER BY timestamp DESC
                LIMIT 1
            ) p ON TRUE
            WHERE pol.item_id = :item_id
              AND pol.source_id = :source_id
            """
        ),
        {"item_id": item_id, "source_id": source_id},
    ).first()
    if row is None:
        return None, None
    return row.price, row.last_observed_at


def _build_meta_info(
    *,
    result: VerdictResult,
    source_a_id: int,
    source_a_name: str,
    source_b_id: int,
    source_b_name: str,
    curated_last_polled_at: datetime | None,
    pricempire_last_polled_at: datetime | None,
) -> dict:
    """Build the JSONB-bound meta_info dict for one insights row.

    Money fields (curated_price, pricempire_price) are serialized as
    strings — MoneyStr discipline; float() of a price is a bug. Drift
    + threshold_used are also serialized as strings since they're
    ratios derived from money. Multipliers and ages stay numeric.
    """
    return {
        "source_a_id": int(source_a_id),
        "source_a_name": source_a_name,
        "source_b_id": int(source_b_id),
        "source_b_name": source_b_name,
        "verdict": result.verdict,
        "classification": result.classification,
        "threshold_used": str(result.threshold_used),
        "threshold_multiplier": result.threshold_multiplier,
        "curated_price": (
            str(result.curated_price)
            if result.curated_price is not None
            else None
        ),
        "pricempire_price": (
            str(result.pricempire_price)
            if result.pricempire_price is not None
            else None
        ),
        "curated_last_polled_at": (
            curated_last_polled_at.isoformat()
            if curated_last_polled_at is not None
            else None
        ),
        "pricempire_last_polled_at": (
            pricempire_last_polled_at.isoformat()
            if pricempire_last_polled_at is not None
            else None
        ),
        "curated_age_min": (
            round(result.curated_age_min, 1)
            if result.curated_age_min is not None
            else None
        ),
        "pricempire_age_min": (
            round(result.pricempire_age_min, 1)
            if result.pricempire_age_min is not None
            else None
        ),
        "note": result.note,
    }
