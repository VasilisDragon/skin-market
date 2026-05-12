"""Per-item, per-source moving averages.

Two insight types produced:

- ``moving_avg_7d``  — arithmetic mean of ``prices.price`` over the last
  7 days for one (item, source) pair.
- ``moving_avg_30d`` — same for 30 days.

The computation is a single Postgres CTE per cycle that groups by
``item_id, source_id`` and emits one row per (item, source) combination
that has any observations in the window. Sources are iterated
**dynamically** from ``sources WHERE enabled``; no source name is
hardcoded anywhere in the SQL. Adding a third (or Nth) source later is
a row insert, not a code change.

At v1 scale (~50 items × 2 sources × hourly cycle ≈ 100 rows/hour into
``insights``), this is trivially small. If volume grows past TS
practical handling, the next step is a TimescaleDB continuous
aggregate over a hypertable view of ``prices``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Windows we compute. Adding a 90d window later is a one-line change.
WINDOWS: tuple[tuple[str, int], ...] = (
    ("moving_avg_7d", 7),
    ("moving_avg_30d", 30),
)


def compute_and_store(session: Session, now: datetime | None = None) -> int:
    """Compute MA rows for every (item, source) pair currently in the DB
    and INSERT them as ``insights`` rows.

    Returns the total number of insight rows written. ``now`` is
    overridable for tests; production uses ``datetime.now(UTC)``.

    The function is idempotent in the weak sense: re-running within the
    same cycle window produces additional rows with very close values
    and a fresh ``computed_at``. Downstream "latest insight" queries
    use ``ORDER BY computed_at DESC LIMIT 1``, so duplicate rows don't
    confuse consumers.
    """
    now = now or datetime.now(UTC)
    written = 0
    for insight_type, days in WINDOWS:
        rows = session.execute(
            text(
                """
                INSERT INTO insights
                    (item_id, computed_at, insight_type, value, meta_info)
                SELECT
                    p.item_id,
                    :now AS computed_at,
                    :itype AS insight_type,
                    AVG(p.price) AS value,
                    jsonb_build_object(
                        'source_id', p.source_id,
                        'source_name', s.name,
                        'window_days', :days,
                        'n_samples', COUNT(*)
                    ) AS meta_info
                FROM prices p
                JOIN sources s ON s.id = p.source_id
                WHERE s.enabled = TRUE
                  AND p.timestamp >= :since
                GROUP BY p.item_id, p.source_id, s.name
                HAVING COUNT(*) >= 1
                """
            ),
            {
                "now": now,
                "itype": insight_type,
                "days": days,
                "since": now - _days_to_interval(days),
            },
        )
        written += rows.rowcount or 0
    return written


def _days_to_interval(days: int):
    """Compute the lower bound for a window. Returns a timedelta that
    SQLAlchemy serializes to a Postgres INTERVAL when bound as a
    parameter.
    """
    from datetime import timedelta

    return timedelta(days=days)


def latest_for_item(
    session: Session,
    item_id,
    insight_types: Iterable[str] = ("moving_avg_7d", "moving_avg_30d"),
) -> list[dict]:
    """Helper used by the bot/API layer: latest one row per
    (insight_type, source) for a given item.

    Not part of the cron path; lives in this module because the SQL is
    co-located with the writer. Returns a list of dicts with keys
    ``insight_type``, ``source_name``, ``value``, ``computed_at``,
    ``n_samples``.
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (i.insight_type, i.meta_info->>'source_id')
                i.insight_type,
                i.meta_info->>'source_name' AS source_name,
                i.value,
                i.computed_at,
                (i.meta_info->>'n_samples')::INT AS n_samples
            FROM insights i
            WHERE i.item_id = :item_id
              AND i.insight_type = ANY(:types)
            ORDER BY
                i.insight_type,
                i.meta_info->>'source_id',
                i.computed_at DESC
            """
        ),
        {"item_id": item_id, "types": list(insight_types)},
    ).mappings()
    return [dict(r) for r in rows]
