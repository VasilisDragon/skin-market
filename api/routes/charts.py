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

## Visual style (Phase 8a)

The chart uses a dark dashboard treatment so Discord users get
something that reads like a financial chart, not a default matplotlib
output. All the visual treatment lives in ``_apply_chart_style``
(below) — one place to tweak when the style needs to evolve. Charts
remain single-source per ADR 014 §6, so the per-source line color
matters for visual identity even though only one source is plotted.

Y-axis is currency-formatted (``$X.XX`` for USD sources, ``X.XX SC``
for Steam wallet credit). X-axis adapts: hours for very short
windows, days for week-scale, weeks for month-scale. Top + right
spines hidden; bottom + left spines thin. Subtle grid behind the
data. Small ``skin-market`` attribution mark bottom-right.
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

# ---------------------------------------------------------------------
# Dark-dashboard theme (Phase 8a). Colors are from the tokyo-night
# palette, chosen for legibility on a dark background and to feel
# coherent across sources. If you tune any of these, update the test
# fixture render saved alongside the commit so the visual reference
# matches.
# ---------------------------------------------------------------------

CHART_BG = "#1a1b26"        # off-black, slightly cool
CHART_FG = "#c0caf5"        # foreground text — light slate, not pure white
CHART_GRID = "#414868"      # grid + soft separators
CHART_AXIS = "#565f89"      # spines + ticks + attribution

# Per-source line colors. Single-source charts (ADR 014 §6) so each
# chart shows exactly one of these, but the consistency across charts
# means a user who's seen Skinport once will recognize the blue.
SOURCE_LINE_COLORS: dict[str, str] = {
    "skinport": "#7aa2f7",      # blue
    "dmarket": "#9ece6a",       # green
    "steam_market": "#e0af68",  # warm amber
}
SOURCE_LINE_FALLBACK = "#bb9af7"  # any new source until added above


def _apply_chart_style(fig, ax, *, source: str, denomination: str, days: int) -> None:
    """Apply skin-market's dark-dashboard style to ``fig`` / ``ax``.

    Centralizes every visual choice so a future style refresh is a
    one-place change. Touches:

    - figure + axes backgrounds (``CHART_BG``)
    - text color (title, labels, ticks)
    - spines (top + right hidden; bottom + left thinned)
    - grid (behind data via ``set_axisbelow(True)``)
    - x-axis date locator + formatter (adapts to ``days``)
    - y-axis currency formatter (``$`` for USD; ``SC`` for wallet credit)
    - attribution text bottom-right
    - layout padding so the data doesn't run flush to the edges

    The data plot itself (``ax.plot``) is the caller's responsibility;
    pass the line color via ``SOURCE_LINE_COLORS[source]``.
    """
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter

    # Backgrounds.
    fig.patch.set_facecolor(CHART_BG)
    ax.set_facecolor(CHART_BG)

    # Font — DejaVu Sans ships with matplotlib (no system font
    # dependency). Sans-serif throughout; matplotlib's default
    # Computer Modern serif reads academic.
    font_family = "DejaVu Sans"
    ax.title.set_fontfamily(font_family)
    ax.xaxis.label.set_fontfamily(font_family)
    ax.yaxis.label.set_fontfamily(font_family)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontfamily(font_family)

    # Text colors.
    ax.title.set_color(CHART_FG)
    ax.title.set_fontsize(13)
    ax.title.set_fontweight("medium")
    ax.xaxis.label.set_color(CHART_FG)
    ax.yaxis.label.set_color(CHART_FG)
    ax.tick_params(colors=CHART_FG, which="both", labelsize=10)

    # Spines.
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(CHART_AXIS)
        ax.spines[side].set_linewidth(0.6)

    # Grid: soft, behind the data line.
    ax.grid(
        True,
        color=CHART_GRID,
        linewidth=0.5,
        alpha=0.4,
        zorder=0,
    )
    ax.set_axisbelow(True)

    # X-axis date locator/formatter — adapt density to window length
    # so the labels don't crowd or get sparse depending on ``days``.
    if days <= 1:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    elif days <= 3:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=12))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M"))
    elif days <= 14:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 7)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    elif days <= 60:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 8)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    else:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=max(1, days // 30)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=30, ha="right")

    # Y-axis currency formatter — raw floats look amateur on a
    # financial chart. Denomination decides the prefix/suffix.
    if denomination == "usd":
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: f"${v:,.2f}")
        )
    elif denomination == "wallet_credit":
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _pos: f"{v:,.2f} SC")
        )
    # Other denominations (future RMB, EUR, etc.) fall back to
    # matplotlib's default formatter — explicit denomination support
    # is a one-line add when a new source lands.

    # Attribution mark — bottom-right, soft so it doesn't fight the
    # data for attention. Drawn on the figure (not axes) so the
    # plot area's tight_layout doesn't shift it.
    fig.text(
        0.99,
        0.01,
        "skin-market",
        ha="right",
        va="bottom",
        color=CHART_AXIS,
        fontsize=8,
        fontfamily=font_family,
        alpha=0.7,
    )

    # Breathing room around the plot. ``tight_layout`` plus a manual
    # pad lifts the labels off the canvas edge.
    fig.tight_layout(pad=2.0)


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

    # Local import — matplotlib is heavy. (~80MB on disk; ~600ms
    # additional interpreter startup.)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    line_color = SOURCE_LINE_COLORS.get(source, SOURCE_LINE_FALLBACK)

    if rows:
        timestamps = [r.timestamp for r in rows]
        prices = [float(r.price) for r in rows]
        ax.plot(
            timestamps,
            prices,
            color=line_color,
            linewidth=1.6,
            marker="o",
            markersize=3,
            markerfacecolor=line_color,
            markeredgecolor=line_color,
            zorder=3,
        )
        # Subtle fill under the curve gives the chart depth without
        # implying area-under-curve semantics — alpha is low so it
        # reads as visual texture, not a stacked-area chart.
        ax.fill_between(
            timestamps, prices, min(prices),
            color=line_color, alpha=0.10, zorder=2,
        )
    else:
        ax.text(
            0.5,
            0.5,
            f"No observations in the last {days} days",
            transform=ax.transAxes,
            ha="center",
            va="center",
            color=CHART_FG,
            fontsize=12,
            fontfamily="DejaVu Sans",
        )

    denomination_label = (
        "USD"
        if meta["denomination"] == "usd"
        else "Steam Wallet credit"
    )
    # Truncate over-long display names so they don't overflow the
    # canvas; the slug is queryable separately if the full name
    # matters for the user.
    title_name = meta["display_name"]
    if len(title_name) > 55:
        title_name = title_name[:52] + "…"
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel(f"Price ({denomination_label})")
    ax.set_title(f"{title_name} · {source} · last {days}d")

    _apply_chart_style(
        fig, ax,
        source=source,
        denomination=meta["denomination"],
        days=days,
    )

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png",
        facecolor=fig.get_facecolor(),  # preserve dark bg in saved file
        edgecolor="none",
    )
    plt.close(fig)  # release figure memory
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")
