"""Anomaly detection.

Two insight types:

- ``volume_anomaly`` — Steam-only. Steam's ``volume`` column is 24-hour
  sales count (a flow). When today's volume is more than N standard
  deviations from the rolling mean for this item, flag it. Skinport's
  ``quantity`` is a stock measurement (current listings count); flow
  anomalies don't generalize to it, so this insight type is intentionally
  scoped to flow-style sources. The query filters by
  ``s.denomination = 'wallet_credit'`` only as a convenient proxy for
  "is this a Steam-style source" — once we add another flow-style
  source, the SQL needs a more direct flag (TODO in code).

- ``cross_source_divergence`` — when the current ``cross_source_spread``
  for a (item, source_a, source_b) pair has moved by more than N
  standard deviations from the rolling baseline of past spreads, that
  pair has diverged. This is the anomaly we care about most for the
  Doppler / Marble Fade / wallet-discount-shift class of events.
  Single-source moves are NOT flagged: a Doppler 4×ing in five minutes
  on Skinport while Steam stays flat is a divergence event; the same
  4× with Steam also moving is general market drift.

Per ADR 010, anomaly detection does not filter or discard "implausible"
single-source observations. The bar for an anomaly is **divergence**,
not absolute magnitude.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# How many stddevs from the rolling mean before we flag.
VOLUME_ANOMALY_Z = 2.0
DIVERGENCE_Z = 2.0

# Lookback windows for the rolling baseline.
VOLUME_BASELINE_DAYS = 7
DIVERGENCE_BASELINE_DAYS = 7

# Minimum sample count before we trust the baseline. With <N samples,
# stddev is noisy and false-positives dominate.
MIN_VOLUME_SAMPLES = 10
MIN_DIVERGENCE_SAMPLES = 10


def compute_and_store(session: Session, now: datetime | None = None) -> int:
    """Compute and insert anomaly rows. Returns total rows written."""
    now = now or datetime.now(UTC)
    written = 0
    written += _detect_volume_anomalies(session, now)
    written += _detect_cross_source_divergence(session, now)
    return written


def _detect_volume_anomalies(session: Session, now: datetime) -> int:
    """Flag items whose most recent Steam volume reading is >Z stddev
    from the rolling mean over the past VOLUME_BASELINE_DAYS days.
    Skinport is excluded because its ``quantity`` is stock, not flow.
    """
    since = now - timedelta(days=VOLUME_BASELINE_DAYS)
    rows = session.execute(
        text(
            """
            WITH series AS (
                SELECT
                    p.item_id,
                    p.source_id,
                    p.timestamp,
                    p.volume::FLOAT AS v
                FROM prices p
                JOIN sources s ON s.id = p.source_id
                WHERE s.enabled = TRUE
                  AND s.denomination = 'wallet_credit'
                  AND p.timestamp >= :since
                  AND p.volume IS NOT NULL
            ),
            stats AS (
                SELECT
                    item_id,
                    source_id,
                    AVG(v) AS mean_v,
                    STDDEV_POP(v) AS std_v,
                    COUNT(*) AS n
                FROM series
                GROUP BY item_id, source_id
                HAVING COUNT(*) >= :min_n
            ),
            latest AS (
                SELECT DISTINCT ON (item_id, source_id)
                    item_id, source_id, v, timestamp
                FROM series
                ORDER BY item_id, source_id, timestamp DESC
            )
            INSERT INTO insights
                (item_id, computed_at, insight_type, value, meta_info)
            SELECT
                l.item_id,
                :now AS computed_at,
                'volume_anomaly' AS insight_type,
                CASE
                    WHEN stats.std_v > 0 THEN (l.v - stats.mean_v) / stats.std_v
                    ELSE 0.0
                END AS value,
                jsonb_build_object(
                    'source_id', l.source_id,
                    'observed_volume', l.v,
                    'baseline_mean', stats.mean_v,
                    'baseline_stddev', stats.std_v,
                    'n_samples', stats.n,
                    'threshold_z', :threshold
                ) AS meta_info
            FROM latest l
            JOIN stats USING (item_id, source_id)
            WHERE stats.std_v > 0
              AND ABS((l.v - stats.mean_v) / stats.std_v) >= :threshold
            """
        ),
        {
            "since": since,
            "now": now,
            "min_n": MIN_VOLUME_SAMPLES,
            "threshold": VOLUME_ANOMALY_Z,
        },
    )
    return rows.rowcount or 0


def _detect_cross_source_divergence(session: Session, now: datetime) -> int:
    """Compare the latest cross_source_spread (from
    ``analytics.cross_source``) against the rolling baseline of past
    spreads for the same (item, source_a, source_b) triple.
    """
    since = now - timedelta(days=DIVERGENCE_BASELINE_DAYS)
    rows = session.execute(
        text(
            """
            WITH spreads AS (
                SELECT
                    item_id,
                    computed_at,
                    value::FLOAT AS spread,
                    meta_info->>'source_a_id' AS source_a_id,
                    meta_info->>'source_b_id' AS source_b_id
                FROM insights
                WHERE insight_type = 'cross_source_spread'
                  AND computed_at >= :since
            ),
            stats AS (
                SELECT
                    item_id, source_a_id, source_b_id,
                    AVG(spread) AS mean_s,
                    STDDEV_POP(spread) AS std_s,
                    COUNT(*) AS n
                FROM spreads
                GROUP BY item_id, source_a_id, source_b_id
                HAVING COUNT(*) >= :min_n
            ),
            latest AS (
                SELECT DISTINCT ON (item_id, source_a_id, source_b_id)
                    item_id, source_a_id, source_b_id, spread, computed_at
                FROM spreads
                ORDER BY item_id, source_a_id, source_b_id, computed_at DESC
            )
            INSERT INTO insights
                (item_id, computed_at, insight_type, value, meta_info)
            SELECT
                l.item_id,
                :now AS computed_at,
                'cross_source_divergence' AS insight_type,
                CASE
                    WHEN stats.std_s > 0 THEN (l.spread - stats.mean_s) / stats.std_s
                    ELSE 0.0
                END AS value,
                jsonb_build_object(
                    'source_a_id', l.source_a_id,
                    'source_b_id', l.source_b_id,
                    'observed_spread', l.spread,
                    'baseline_mean', stats.mean_s,
                    'baseline_stddev', stats.std_s,
                    'n_samples', stats.n,
                    'threshold_z', :threshold
                ) AS meta_info
            FROM latest l
            JOIN stats USING (item_id, source_a_id, source_b_id)
            WHERE stats.std_s > 0
              AND ABS((l.spread - stats.mean_s) / stats.std_s) >= :threshold
            """
        ),
        {
            "since": since,
            "now": now,
            "min_n": MIN_DIVERGENCE_SAMPLES,
            "threshold": DIVERGENCE_Z,
        },
    )
    return rows.rowcount or 0
