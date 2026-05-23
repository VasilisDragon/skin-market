"""Daily narrative insight: one English paragraph summarizing the day's
notable market moves.

Runs nightly via the analytics scheduler. The job:

1. Identifies "newsworthy" items from the last 24h:
   - Top N items by absolute price change ratio per source
   - Items with a recent ``volume_anomaly`` insight
   - Items with a recent ``cross_source_divergence`` insight
2. Builds a structured JSON payload of names + numbers + denominations.
3. Prompts DeepSeek with explicit anti-hallucination guardrails:
   "use only the items in the data; do not invent items; denote each
   price with its denomination."
4. Stores the response as one ``insights`` row:
   - ``insight_type = 'daily_narrative'``
   - ``text_value`` = the paragraph
   - ``meta_info`` = the structured payload that was passed to the LLM
     (citation / audit trail so a reviewer can fact-check the prose
     against the data the model saw)

Failure mode: if DeepSeek returns an error or the prose is empty, the job
logs ERROR and inserts nothing. Tomorrow's run will try again. The bot
gracefully handles "no narrative for today" by falling back to raw
numbers.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from analytics.deepseek_client import DeepSeekError, chat

logger = logging.getLogger(__name__)


# How many items per category to surface to the LLM. Tight cap so the
# prompt stays small (latency + accuracy both benefit).
TOP_N_MOVERS = 8
RECENT_ANOMALY_HOURS = 36


_SYSTEM_PROMPT = """\
You are a CS2 skin market analyst writing a brief daily-recap paragraph
for a Discord channel. Your audience is enthusiast traders who want
specific named items, specific percentages, and the source/denomination
context — not a generic market overview.

Rules:
- Mention ONLY items present in the data block. Do NOT invent items,
  prices, or moves.
- Use the exact item names from the data. Do not abbreviate or rename.
- Every price you cite MUST include its denomination tag (USD vs
  wallet credit). A Steam wallet-credit price is NOT a dollar amount.
- Output ONE paragraph, 4-7 sentences, plain prose. No bullet points.
  No headers. No markdown.
- If the data is sparse, write a short honest paragraph saying so.
"""


def _gather_newsworthy(
    session: Session, now: datetime
) -> dict:
    """Pull the items that should be cited in tonight's narrative."""
    since_movers = now - timedelta(hours=24)
    since_anomalies = now - timedelta(hours=RECENT_ANOMALY_HOURS)

    # Top price movers per source (by ratio first/last in the window).
    movers = session.execute(
        text(
            """
            WITH window_rows AS (
                SELECT
                    p.item_id,
                    p.source_id,
                    s.name AS source_name,
                    s.denomination,
                    p.price,
                    p.timestamp,
                    i.market_hash_name,
                    FIRST_VALUE(p.price) OVER (
                        PARTITION BY p.item_id, p.source_id
                        ORDER BY p.timestamp ASC
                    ) AS first_price,
                    FIRST_VALUE(p.price) OVER (
                        PARTITION BY p.item_id, p.source_id
                        ORDER BY p.timestamp DESC
                    ) AS last_price
                FROM prices p
                JOIN sources s ON s.id = p.source_id
                JOIN items i ON i.id = p.item_id
                WHERE s.enabled = TRUE
                  AND p.timestamp >= :since
            ),
            agg AS (
                SELECT DISTINCT
                    market_hash_name,
                    source_name,
                    denomination,
                    first_price,
                    last_price,
                    CASE
                        WHEN first_price = 0 THEN NULL
                        ELSE (last_price - first_price) / first_price
                    END AS change_ratio
                FROM window_rows
            )
            SELECT *
            FROM agg
            WHERE change_ratio IS NOT NULL
            ORDER BY ABS(change_ratio) DESC
            LIMIT :n
            """
        ),
        {"since": since_movers, "n": TOP_N_MOVERS},
    ).mappings().all()

    # Recent volume anomalies.
    volume_anomalies = session.execute(
        text(
            """
            SELECT DISTINCT ON (i.id)
                i.market_hash_name,
                insights.value AS z,
                insights.meta_info,
                insights.computed_at
            FROM insights
            JOIN items i ON i.id = insights.item_id
            WHERE insights.insight_type = 'volume_anomaly'
              AND insights.computed_at >= :since
            ORDER BY i.id, insights.computed_at DESC
            """
        ),
        {"since": since_anomalies},
    ).mappings().all()

    # Recent cross-source divergences.
    divergences = session.execute(
        text(
            """
            SELECT DISTINCT ON (i.id)
                i.market_hash_name,
                insights.value AS z,
                insights.meta_info,
                insights.computed_at
            FROM insights
            JOIN items i ON i.id = insights.item_id
            WHERE insights.insight_type = 'cross_source_divergence'
              AND insights.computed_at >= :since
            ORDER BY i.id, insights.computed_at DESC
            """
        ),
        {"since": since_anomalies},
    ).mappings().all()

    return {
        "as_of": now.isoformat(),
        "top_movers": [
            {
                "name": row["market_hash_name"],
                "source": row["source_name"],
                "denomination": row["denomination"],
                "open_price": str(row["first_price"]),
                "close_price": str(row["last_price"]),
                "change_ratio": float(row["change_ratio"]),
            }
            for row in movers
        ],
        "volume_anomalies": [
            {
                "name": row["market_hash_name"],
                "z_score": float(row["z"]) if row["z"] is not None else None,
                "details": dict(row["meta_info"] or {}),
            }
            for row in volume_anomalies
        ],
        "cross_source_divergences": [
            {
                "name": row["market_hash_name"],
                "z_score": float(row["z"]) if row["z"] is not None else None,
                "details": dict(row["meta_info"] or {}),
            }
            for row in divergences
        ],
    }


def generate_and_store(session: Session, now: datetime | None = None) -> bool:
    """Run the narrative job. Returns True if a row was inserted."""
    now = now or datetime.now(UTC)
    payload = _gather_newsworthy(session, now)

    total = (
        len(payload["top_movers"])
        + len(payload["volume_anomalies"])
        + len(payload["cross_source_divergences"])
    )
    if total == 0:
        logger.info("Narrative job: no newsworthy data; skipping LLM call")
        return False

    user_prompt = (
        "Data block (JSON):\n\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Write the daily-recap paragraph now."
    )

    try:
        narrative = chat(_SYSTEM_PROMPT, user_prompt).strip()
    except DeepSeekError as exc:
        logger.error("Narrative job: DeepSeek call failed: %s", exc)
        return False

    if not narrative:
        logger.error("Narrative job: model returned empty text")
        return False

    # Insert one row. text_value holds the prose, meta_info holds the
    # citation payload so a reviewer can fact-check.
    session.execute(
        text(
            """
            INSERT INTO insights
                (item_id, computed_at, insight_type, value, text_value, meta_info)
            VALUES (
                -- A narrative isn't about one item; we point it at the
                -- first cited item (or any item, doesn't matter for the
                -- bot's lookup pattern). The bot queries by insight_type,
                -- not by item_id, for narratives.
                (SELECT id FROM items ORDER BY created_at ASC LIMIT 1),
                :now, 'daily_narrative', NULL, :text_value,
                CAST(:meta AS jsonb)
            )
            """
        ),
        {
            "now": now,
            "text_value": narrative,
            "meta": json.dumps(payload),
        },
    )
    session.commit()
    logger.info(
        "Narrative job: stored daily_narrative (%d chars, %d items cited)",
        len(narrative),
        total,
    )
    return True
