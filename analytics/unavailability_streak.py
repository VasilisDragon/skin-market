"""Per-(item, source) unavailability streak insight.

Phase 7a — the operator-visibility counter the user asked for after the
Phase 5 ADR-013 conversation: when an item has been missing from a
source for several consecutive analytics cycles, surface that as
``insight_type = 'item_unavailability_streak'`` with the streak length
as ``value``. The 14-item Steam "ultra-rare tail" (Dragon Lores, Howl,
Fade knives, special gloves) are the persistent case; transient
flickers (Vulcan / Lightning Strike / Crimson Web) accumulate short
streaks that reset on observation.

This is **additive** to the existing ``unavailable`` vs ``declined``
cycle-counter semantics (ADR 013) — it doesn't change what counts as
unavailable inside a collector cycle. It just exposes how long an
item has been in that state at the analytics layer so the bot can
render *"Steam: unavailable for N cycles"* instead of silently omitting
the source.

## Algorithm

For each ``(item, source)`` pair where the source is enabled:

1. Find the latest ``prices.timestamp`` for that pair.
2. Apply a grace window of ``source.interval_minutes × GRACE_FACTOR``
   (default ``1.5``). One missed collector cycle plus jitter is normal
   and shouldn't count.
3. If the latest observation is inside the grace window: the item is
   "currently observed" — skip, emit no row.
4. Otherwise the pair is "missing this cycle". Look up the most recent
   ``item_unavailability_streak`` row for the same ``(item, source)``:
   - If its ``meta.last_seen_observed`` matches the current pair's
     latest observation timestamp (or both are NULL for never-observed
     items), this is a **continuation** → ``streak = prev.value + 1``,
     ``first_seen_unavailable`` carries forward from prev.
   - Otherwise (there was an intervening observation since the last
     streak row, or no prior streak row): **new streak** →
     ``streak = 1``, ``first_seen_unavailable = now``.
5. Insert one row with ``value = streak`` and ``meta`` carrying
   ``source_id``, ``source_name``, ``source_interval_minutes``,
   ``streak_cycles`` (duplicated for JSON consumers), ``first_seen_unavailable``,
   ``last_seen_observed``.

## Why not emit rows for currently-observed pairs

48 items × 3 sources × 24 cycles/day × 365 days = ~1.26M rows/year of
``streak = 0`` no-ops if we wrote them. The bot's "is this item
available?" query becomes "is there a streak row whose ``computed_at``
is within the last analytics cycle's grace window?" — sparse storage,
same answer.

## Why a fresh insight type rather than a column on ``items``

- ``items`` is metadata (intrinsic), not operational state. Adding
  transient state there conflates the two.
- ``insights`` rows give us a time-series for free: a streak that
  starts, grows, ends gives a graphable shape on the insights table.
- Per-source granularity is natural (one row per pair); a column on
  ``items`` would need one column per source — schema change per new
  source.
- Adding a fourth source is still a row insert, not a schema change.

Hooked into ``analytics/scheduler.py`` next to ``cross_source`` and
``moving_averages``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Multiplier on ``source.interval_minutes`` for the "still fresh" grace
# window. 1.5× is small enough that a multi-cycle outage starts
# accumulating a streak quickly, large enough that a single missed
# cycle plus jitter doesn't trip the counter.
GRACE_FACTOR: float = 1.5


def compute_and_store(
    session: Session, now: datetime | None = None
) -> int:
    """Compute streak rows for every (item, enabled source) pair that
    is currently missing. Returns the number of rows written."""
    now = now or datetime.now(UTC)

    sources = (
        session.execute(
            text(
                "SELECT id, name, interval_minutes "
                "FROM sources WHERE enabled = TRUE ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    items = (
        session.execute(
            text("SELECT id, market_hash_name FROM items ORDER BY id")
        )
        .mappings()
        .all()
    )

    written = 0
    for source in sources:
        grace = timedelta(
            minutes=source["interval_minutes"] * GRACE_FACTOR
        )
        stale_threshold = now - grace

        # One query per source: per-item last-polled timestamp from
        # observation_log. NOT from prices — prices is dedup'd (ADR 009
        # §3), so a stable-price item's prices.timestamp doesn't
        # advance even when the collector observes it every cycle.
        # observation_log advances on every successful poll regardless
        # of whether a prices row was written. ADR 015.
        latest_obs_rows = (
            session.execute(
                text(
                    """
                    SELECT item_id, last_observed_at AS timestamp
                    FROM observation_log
                    WHERE source_id = :source_id
                    """
                ),
                {"source_id": source["id"]},
            )
            .mappings()
            .all()
        )
        last_obs_by_item: dict = {
            row["item_id"]: row["timestamp"] for row in latest_obs_rows
        }

        # One query per source: latest streak row per item.
        prev_streak_rows = (
            session.execute(
                text(
                    """
                    SELECT DISTINCT ON (item_id)
                        item_id, value, meta_info
                    FROM insights
                    WHERE insight_type = 'item_unavailability_streak'
                      AND (meta_info->>'source_id')::INTEGER = :source_id
                    ORDER BY item_id, computed_at DESC
                    """
                ),
                {"source_id": source["id"]},
            )
            .mappings()
            .all()
        )
        prev_streak_by_item: dict = {
            row["item_id"]: row for row in prev_streak_rows
        }

        for item in items:
            last_obs = last_obs_by_item.get(item["id"])

            if last_obs is not None and last_obs > stale_threshold:
                # Currently observed — emit nothing.
                continue

            prev = prev_streak_by_item.get(item["id"])
            last_obs_iso = (
                last_obs.isoformat() if last_obs is not None else None
            )
            if prev is not None and prev["meta_info"].get(
                "last_seen_observed"
            ) == last_obs_iso:
                # Continuation of an existing streak — no observation
                # has landed since the previous streak row was written.
                streak = int(prev["value"]) + 1
                first_seen = prev["meta_info"].get(
                    "first_seen_unavailable", now.isoformat()
                )
            else:
                # Either no prior streak row, or the prior row was
                # written before an intervening observation. Fresh
                # streak starts now.
                streak = 1
                first_seen = now.isoformat()

            meta = {
                "source_id": source["id"],
                "source_name": source["name"],
                "source_interval_minutes": source["interval_minutes"],
                "streak_cycles": streak,
                "first_seen_unavailable": first_seen,
                "last_seen_observed": last_obs_iso,
            }
            session.execute(
                text(
                    """
                    INSERT INTO insights
                        (item_id, computed_at, insight_type, value,
                         meta_info)
                    VALUES
                        (:item_id, :now,
                         'item_unavailability_streak',
                         :value, CAST(:meta AS jsonb))
                    """
                ),
                {
                    "item_id": item["id"],
                    "now": now,
                    "value": streak,
                    "meta": json.dumps(meta),
                },
            )
            written += 1

    return written
