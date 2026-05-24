"""Cross-source views and spreads.

Two insight types:

- ``cross_source_view``: a per-item snapshot listing each source's
  latest price along with its ``denomination``. The bot consumes this
  to render answers like "$28 USD on Skinport / $42 in Steam wallet
  credit" — never averaging or picking one as canonical. See
  ``docs/sources-and-semantics.md`` for why.
- ``cross_source_spread``: a per-pair, time-series number capturing
  ``(price_a - price_b) / price_b`` for each pair of enabled sources.
  Anomaly detection compares the spread against its rolling baseline
  to flag divergence events (Doppler-style variant shifts on
  one source, wallet-credit liquidity changes on Steam, etc.).

Sources iterated dynamically from ``sources WHERE enabled``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from itertools import combinations

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Latest price is "last row seen within this lookback". An item with no
# observation in this window is considered stale and skipped — emitting
# a spread row using a 2-week-old price would be misleading.
LATEST_PRICE_LOOKBACK = timedelta(hours=6)


def _latest_prices_by_item_source(session: Session, now: datetime) -> dict:
    """Return ``{(item_id, source_id): (price, source_name, denomination)}``
    for the most-recent row per (item, source) inside ``LATEST_PRICE_LOOKBACK``.
    """
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (p.item_id, p.source_id)
                p.item_id,
                p.source_id,
                p.price,
                s.name AS source_name,
                s.denomination
            FROM prices p
            JOIN sources s ON s.id = p.source_id
            WHERE s.enabled = TRUE
              AND p.timestamp >= :since
            ORDER BY p.item_id, p.source_id, p.timestamp DESC
            """
        ),
        {"since": now - LATEST_PRICE_LOOKBACK},
    ).mappings()
    return {
        (r["item_id"], r["source_id"]): (
            r["price"],
            r["source_name"],
            r["denomination"],
        )
        for r in rows
    }


def compute_and_store(session: Session, now: datetime | None = None) -> int:
    """Compute and insert cross_source_view + cross_source_spread rows.

    Returns total insight rows written. Each item with at least one
    fresh price gets ONE ``cross_source_view`` row. Each item with at
    least two fresh prices from different sources gets one
    ``cross_source_spread`` row per pair (N items × C(sources, 2) pairs).
    """
    now = now or datetime.now(UTC)
    by_pair = _latest_prices_by_item_source(session, now)

    # Group by item.
    items_to_sources: dict = {}
    for (item_id, source_id), (price, source_name, denom) in by_pair.items():
        items_to_sources.setdefault(item_id, []).append(
            {
                "source_id": source_id,
                "source_name": source_name,
                "denomination": denom,
                "price": str(price),  # JSONB friendly
            }
        )

    written = 0
    for item_id, source_list in items_to_sources.items():
        # cross_source_view: one row, all sources for this item.
        view_meta = {"sources": source_list}
        session.execute(
            text(
                """
                INSERT INTO insights
                    (item_id, computed_at, insight_type, value, meta_info)
                VALUES (
                    :item_id, :now, 'cross_source_view', NULL,
                    CAST(:meta AS jsonb)
                )
                """
            ),
            {
                "item_id": item_id,
                "now": now,
                "meta": _json_dumps(view_meta),
            },
        )
        written += 1

        # cross_source_spread: one row per pair of sources.
        for a, b in combinations(source_list, 2):
            price_a, price_b = float(a["price"]), float(b["price"])
            if price_b == 0:
                continue  # avoid div-by-zero; vanishingly rare
            spread_ratio = (price_a - price_b) / price_b
            spread_meta = {
                "source_a_id": a["source_id"],
                "source_a_name": a["source_name"],
                "source_a_price": a["price"],
                "source_a_denomination": a["denomination"],
                "source_b_id": b["source_id"],
                "source_b_name": b["source_name"],
                "source_b_price": b["price"],
                "source_b_denomination": b["denomination"],
            }
            # Dump the JSON in Python and CAST as jsonb. psycopg 3 can't
            # infer types for mixed-type parameters inside
            # jsonb_build_object(), so the single-string-then-cast
            # pattern is the path of least friction.
            session.execute(
                text(
                    """
                    INSERT INTO insights
                        (item_id, computed_at, insight_type, value, meta_info)
                    VALUES (
                        :item_id, :now, 'cross_source_spread', :value,
                        CAST(:meta AS jsonb)
                    )
                    """
                ),
                {
                    "item_id": item_id,
                    "now": now,
                    "value": spread_ratio,
                    "meta": _json_dumps(spread_meta),
                },
            )
            written += 1
    return written


def _json_dumps(obj) -> str:
    import json

    return json.dumps(obj)
