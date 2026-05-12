"""``/items/{slug}/chart`` — PNG price chart for one item × one source.

Returns raw PNG bytes via FastAPI's ``Response`` with
``media_type="image/png"``. Discord renders PNG attachments inline;
the bot can attach the response body directly without base64/JSON
unwrap. ADR 014 §6 — alternative was data-URI in JSON; PNG is simpler
on both sides.

matplotlib is imported **inside the handler**, not at module level.
The library is ~80MB on disk and adds ~600ms to interpreter startup;
deferring keeps the API process's cold-start fast for the common case
(no one asked for a chart on this request).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.connection import get_engine

router = APIRouter(tags=["charts"])

CHART_DEFAULT_DAYS = 7
CHART_DEFAULT_SOURCE = "skinport"
CHART_MAX_DAYS = 90


@router.get("/items/{slug}/chart")
def get_chart(
    slug: str,
    source: Annotated[
        str,
        Query(
            description=(
                "Source name to plot. Charts are single-source by "
                "design — denominations differ across sources, so "
                "superimposing them would lie about the y-axis."
            ),
        ),
    ] = CHART_DEFAULT_SOURCE,
    days: Annotated[
        int,
        Query(
            ge=1,
            le=CHART_MAX_DAYS,
            description=(
                f"Window in days. Default {CHART_DEFAULT_DAYS}, "
                f"max {CHART_MAX_DAYS}."
            ),
        ),
    ] = CHART_DEFAULT_DAYS,
) -> Response:
    now = datetime.now(UTC)
    since = now - timedelta(days=days)

    engine = get_engine()
    with Session(engine) as session:
        meta = session.execute(
            text(
                """
                SELECT
                    i.display_name,
                    s.id AS source_id,
                    s.denomination
                FROM items i
                CROSS JOIN sources s
                WHERE i.slug = :slug AND s.name = :source
                """
            ),
            {"slug": slug, "source": source},
        ).mappings().first()
        if meta is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Unknown item or source: slug={slug!r}, "
                    f"source={source!r}"
                ),
            )

        rows = session.execute(
            text(
                """
                SELECT p.timestamp, p.price
                FROM prices p
                JOIN items i ON i.id = p.item_id
                WHERE i.slug = :slug
                  AND p.source_id = :source_id
                  AND p.timestamp >= :since
                ORDER BY p.timestamp ASC
                """
            ),
            {
                "slug": slug,
                "source_id": meta["source_id"],
                "since": since,
            },
        ).all()

    # Local import — matplotlib is heavy.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

    fig, ax = plt.subplots(figsize=(10, 5), dpi=90)
    if rows:
        timestamps = [r.timestamp for r in rows]
        prices = [float(r.price) for r in rows]
        ax.plot(
            timestamps,
            prices,
            marker="o",
            linestyle="-",
            markersize=2,
            linewidth=1,
        )
    else:
        ax.text(
            0.5,
            0.5,
            f"No observations in the last {days} days",
            transform=ax.transAxes,
            ha="center",
            va="center",
        )

    denomination_label = (
        "USD"
        if meta["denomination"] == "usd"
        else "Steam Wallet credit"
    )
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel(f"Price ({denomination_label})")
    ax.set_title(
        f"{meta['display_name']} — {source} — last {days}d"
    )
    ax.grid(True, linestyle="--", alpha=0.5)
    if rows:
        ax.xaxis.set_major_formatter(DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)  # release figure memory
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")
