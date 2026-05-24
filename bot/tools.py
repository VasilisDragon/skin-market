"""HTTP tool wrappers used by the Discord bot's LLM router."""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Inside compose the bot reaches the API by service name and container
# port. Host port mappings are only for local development.
DEFAULT_API_BASE_URL = "http://api:8000"

EXPECTED_SOURCES: tuple[str, ...] = ("skinport", "dmarket", "steam_market")

_DENOMINATION_BY_SOURCE: dict[str, str] = {
    "steam_market": "wallet_credit",
    "skinport": "usd",
    "dmarket": "usd",
}

STALE_HOURS: int = 4
ANOMALY_FRESHNESS_HOURS: int = 2

# Keep tool payloads bounded before they are passed back into the LLM.
HISTORY_DOWNSAMPLE_THRESHOLD: int = 30
ANOMALIES_TOP_N_THRESHOLD: int = 10
WATCHLIST_SAMPLE_SIZE: int = 5

# Pre-composed tier notes keep user-facing framing deterministic.
_TIER_NOTE_FEATURED: str = (
    "This item is on the featured watchlist — we track it but with "
    "less detail than our priority (curated) items. Detailed drift "
    "checks aren't available for this tier."
)


def _tier_note_substrate(active_wear_display_name: str | None) -> str:
    """Compose the note for items outside the active watchlist."""
    if active_wear_display_name is None:
        return (
            "This item isn't on our actively-tracked watchlist. "
            "Pricempire-side data may still be available via direct "
            "lookup, but no curated/featured cycle is producing "
            "current prices for it."
        )
    return (
        f"This wear isn't on our actively-tracked watchlist. The "
        f"currently-tracked wear is {active_wear_display_name}. I "
        f"can show historical data for the non-tracked wear, or you "
        f"can ask about the active wear instead."
    )


# Mapping from drift verdict to deterministic user-facing framing.
_DRIFT_FRAMING_TEMPLATES: dict[str, dict] = {
    "drift_alert": {
        "show_number": True,
        "template": (
            "Drift vs Pricempire: {curated_name} is {signed_pct} vs "
            "{pricempire_name} (threshold ±{threshold_pct})."
        ),
    },
    "no_drift": {
        "show_number": True,
        "template": (
            "Cross-check vs Pricempire: spread within tolerance "
            "({signed_pct}, threshold ±{threshold_pct})."
        ),
    },
    "pattern_skip": {
        "show_number": False,
        "template": (
            "Pattern-bearing item (phase/seed variation) — drift "
            "check skipped by design; Pricempire and our direct "
            "source name the same listing as different things."
        ),
    },
    "stale_curated": {
        "show_number": False,
        "template": (
            "Drift check inconclusive: our {curated_name} reading "
            "is stale ({curated_age_min:.0f} min old)."
        ),
    },
    "stale_pricempire": {
        "show_number": False,
        "template": (
            "Drift check inconclusive: Pricempire's "
            "{pricempire_name} reading is stale "
            "({pricempire_age_min:.0f} min old)."
        ),
    },
    "stale_both": {
        "show_number": False,
        "template": (
            "Drift check inconclusive: both our {curated_name} and "
            "Pricempire's {pricempire_name} readings are stale."
        ),
    },
    "no_comparable_data": {
        "show_number": False,
        "template": (
            "Drift check warming up: no comparable Pricempire data "
            "yet for this item."
        ),
    },
}


@dataclass(frozen=True)
class Attachment:
    """Binary tool output — e.g. ``render_chart`` returns a PNG.
    ``bot.discord_render`` wraps this into a ``discord.File`` for
    Discord upload."""

    content: bytes
    media_type: str
    filename: str


class SkinMarketBotError(Exception):
    """Base class."""


class ApiUnreachableError(SkinMarketBotError):
    """Network-level failure: the api at ``base_url`` didn't respond."""


class ApiAuthError(SkinMarketBotError):
    """401 from the api — token mismatch or auth misconfigured."""


class ItemNotInWatchlistError(SkinMarketBotError):
    """404 on /items/{slug}/… — item not on the watchlist (or, for
    ``narrative_today``, no narrative row exists yet)."""


class ApiUnexpectedError(SkinMarketBotError):
    """5xx or other unexpected HTTP status from the api."""


def _client(timeout_read: float = 30.0) -> httpx.Client:
    token = (os.environ.get("SKIN_MARKET_API_TOKEN") or "").strip()
    if not token:
        raise ApiAuthError(
            "SKIN_MARKET_API_TOKEN environment variable is not set. "
            "Set it to a token from the api container's accepted "
            "set."
        )
    base_url = (
        os.environ.get("SKIN_MARKET_API_BASE_URL")
        or DEFAULT_API_BASE_URL
    )
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(
            connect=5.0, read=timeout_read, write=5.0, pool=5.0
        ),
    )


def _get_json(
    client: httpx.Client, path: str, *, params: dict | None = None
) -> Any:
    try:
        resp = client.get(path, params=params)
    except httpx.RequestError as exc:
        raise ApiUnreachableError(
            f"Couldn't reach the skin-market API at "
            f"{client.base_url!s}: {exc}"
        ) from exc

    if resp.status_code == 401:
        raise ApiAuthError(
            "API rejected the bearer token (401). Verify "
            "SKIN_MARKET_API_TOKEN."
        )
    if resp.status_code == 404:
        raise ItemNotInWatchlistError(
            f"Not found on the api: {path}. The item may not be on "
            f"the watchlist yet."
        )
    if resp.status_code >= 400:
        raise ApiUnexpectedError(
            f"Unexpected {resp.status_code} from {path}: "
            f"{resp.text[:200]}"
        )
    return resp.json()


_items_cache: list[dict] | None = None


def _refresh_items_cache(items: list[dict] | None) -> None:
    """Set or clear the items cache. Tests pass a fixture list;
    passing None forces a fresh API fetch on the next access."""
    global _items_cache
    _items_cache = items


def _get_items_cache() -> list[dict]:
    """Return the items cache, fetching from the API on first call.

    Failures bubble up the typed exception hierarchy so the bot's
    tool-execution loop renders a sensible error rather than
    silently returning ``[]`` (which would suppress the
    active_wear_hint feature without explanation)."""
    global _items_cache
    if _items_cache is None:
        with _client() as c:
            _items_cache = _get_json(c, "/items")
    return _items_cache


def _find_active_wear(substrate_slug: str) -> dict | None:
    """Given a non-curated slug (featured or substrate), find the
    actively-tracked sibling wear (same weapon + skin + StatTrak/
    Souvenir flags, different wear, ``tier == "curated"``). Returns
    ``{"slug": …, "display_name": …}`` or None.

    Structured item fields avoid brittle parsing of display names for
    StatTrak, Souvenir, knives, and gloves."""
    cache = _get_items_cache()
    target = next(
        (row for row in cache if row.get("slug") == substrate_slug),
        None,
    )
    if target is None:
        return None
    weapon = target.get("weapon_name")
    skin = target.get("skin_name")
    stt = target.get("is_stattrak", False)
    sv = target.get("is_souvenir", False)
    if weapon is None or skin is None:
        return None
    for row in cache:
        if row.get("slug") == substrate_slug:
            continue
        if row.get("tier") != "curated":
            continue
        if (
            row.get("weapon_name") == weapon
            and row.get("skin_name") == skin
            and row.get("is_stattrak", False) == stt
            and row.get("is_souvenir", False) == sv
        ):
            return {
                "slug": row.get("slug"),
                "display_name": row.get("display_name"),
            }
    return None


def _format_drift_pct(drift_str: str | None) -> str | None:
    """Convert a signed-ratio string like "-0.1234" into "-12.3%".
    None passes through. Quantizing to one decimal place keeps the
    rendered prose readable."""
    if drift_str is None:
        return None
    ratio = float(drift_str)
    pct = ratio * 100.0
    sign = "+" if pct >= 0 else "-"
    return f"{sign}{abs(pct):.1f}%"


def _format_threshold_pct(threshold_str: str) -> str:
    """Threshold is always unsigned ("0.10" → "10.0%")."""
    return f"{float(threshold_str) * 100:.1f}%"


def _shape_drift_pair(raw_pair: dict) -> dict:
    """Map one /drift API pair entry → drift_summary list entry.

    Adds a pre-formatted ``drift_pct``, a pre-composed ``framing``
    string the LLM renders verbatim, and a ``stale_side`` hint for
    the stale_* verdicts."""
    verdict = raw_pair.get("verdict", "")
    drift_pct = _format_drift_pct(raw_pair.get("drift"))
    template_info = _DRIFT_FRAMING_TEMPLATES.get(verdict, {})
    framing_template = template_info.get(
        "template", "Drift verdict: {verdict}."
    )
    threshold_pct = _format_threshold_pct(
        raw_pair.get("threshold_used", "0.10")
    )
    framing = framing_template.format(
        signed_pct=drift_pct or "n/a",
        threshold_pct=threshold_pct,
        curated_name=raw_pair.get("source_a", "curated"),
        pricempire_name=raw_pair.get("source_b", "pricempire"),
        curated_age_min=raw_pair.get("curated_age_min") or 0.0,
        pricempire_age_min=raw_pair.get("pricempire_age_min") or 0.0,
        verdict=verdict,
    )
    stale_side: str | None = None
    if verdict == "stale_curated":
        stale_side = "curated"
    elif verdict == "stale_pricempire":
        stale_side = "pricempire"
    elif verdict == "stale_both":
        stale_side = "both"
    return {
        "source_a": raw_pair.get("source_a"),
        "source_b": raw_pair.get("source_b"),
        "verdict": verdict,
        "drift_pct": drift_pct,
        "framing": framing,
        "stale_side": stale_side,
        "classification": raw_pair.get("classification"),
        "computed_at": raw_pair.get("computed_at"),
        "curated_price": raw_pair.get("curated_price"),
        "pricempire_price": raw_pair.get("pricempire_price"),
    }


def _is_pricempire_pair(meta: dict) -> bool:
    """Return True when this insight meta references a Pricempire
    sub-provider on either side of the pair. Used by
    query_current_price to filter cross_source_divergence rows that
    accidentally involve Pricempire (none today by construction
    per analytics/drift.py:52-60; the filter is defense-in-depth
    against future schema changes that could let Pricempire spreads
    leak into the legacy anomaly_flag)."""
    sa = meta.get("source_a_name", "")
    sb = meta.get("source_b_name", "")
    return sa.startswith("pricempire_") or sb.startswith("pricempire_")


def list_watchlist() -> dict:
    """Return a **summarized** view of the watchlist for LLM
    consumption: ``{count, by_category, sample}``.

    Categories are inferred client-side via ``_category(display_name)``
    — a heuristic over CS2 weapon names. Mis-categorized items go
    into ``other``; no data-correctness consequence.
    """
    with _client() as c:
        items = _get_json(c, "/items")
    return _summarize_watchlist(items)


def query_current_price(slug: str) -> dict:
    """Return per-source prices + freshness + (for curated-tier items)
    drift_summary + anomaly_flag.

    - ``drift_summary``: per-pair drift verdict from
      ``/items/{slug}/drift``. Present only for curated-tier items
      (featured/substrate get tier_note instead).
    - ``tier_note``: pre-composed user-facing copy for featured/substrate
      tiers. Absent for curated.
    - ``active_wear_hint``: ``{slug, display_name}`` of the
      currently-tracked sibling wear when the queried slug is substrate
      AND a curated-tier sibling exists. Absent otherwise.

    The ``anomaly_flag`` (legacy cross_source_divergence rendering)
    is filtered to non-Pricempire pairs only — defense-in-depth
    against future schema changes; today drift_verdict and
    cross_source_divergence are disjoint by construction per
    analytics/drift.py:52-60.
    """
    with _client() as c:
        price_data = _get_json(c, f"/items/{slug}/price")
        insights_data = _get_json(c, f"/items/{slug}/insights")
        tier = price_data.get("tier", "curated")
        drift_data: dict | None = None
        if tier == "curated":
            # Analytics computes drift only for curated items.
            drift_data = _get_json(c, f"/items/{slug}/drift")

    fresh_by_source: dict[str, dict] = {
        s["source"]: s for s in price_data["sources"]
    }
    divergence_rows: list[dict] = []

    now = datetime.now(UTC)
    for insight in insights_data["insights"]:
        meta = insight.get("meta") or {}
        if insight["insight_type"] == "cross_source_divergence":
            computed_at = _parse_iso(insight["computed_at"])
            age_h = (now - computed_at).total_seconds() / 3600
            if age_h > ANOMALY_FRESHNESS_HOURS:
                continue
            # Pricempire pairs render through drift_summary instead.
            if _is_pricempire_pair(meta):
                continue
            divergence_rows.append(insight)

    per_source: list[dict] = []
    for source_name in EXPECTED_SOURCES:
        if source_name in fresh_by_source:
            row = fresh_by_source[source_name]
            last_polled_at = _parse_iso(row["last_polled_at"])
            minutes_since_polled = int(
                (now - last_polled_at).total_seconds() / 60
            )
            state = (
                "stale"
                if minutes_since_polled > STALE_HOURS * 60
                else "fresh"
            )
            entry: dict[str, Any] = {
                "source": source_name,
                "denomination": row["denomination"],
                "state": state,
                "price": row["price"],
                "volume": row["volume"],
                "last_polled_at": row["last_polled_at"],
                "minutes_since_polled": minutes_since_polled,
            }
            # last_changed_at is informational only — surface it
            # explicitly when the price has been flat for an
            # interesting stretch (>1h beyond last_polled_at), so the
            # bot can mention "price flat for Nh" without the model
            # mistaking it for a freshness warning.
            last_changed_raw = row.get("last_changed_at")
            if last_changed_raw is not None:
                entry["last_changed_at"] = last_changed_raw
                last_changed_at = _parse_iso(last_changed_raw)
                gap_minutes = int(
                    (last_polled_at - last_changed_at).total_seconds() / 60
                )
                if gap_minutes >= 60:
                    entry["price_flat_minutes"] = gap_minutes
            per_source.append(entry)
        else:
            per_source.append(
                {
                    "source": source_name,
                    "denomination": _DENOMINATION_BY_SOURCE.get(
                        source_name
                    ),
                    "state": "never_observed",
                }
            )

    anomaly_flag: dict | None = None
    if divergence_rows:
        worst = max(
            divergence_rows,
            key=lambda r: abs(float(r.get("value") or 0)),
        )
        meta = worst.get("meta") or {}
        anomaly_flag = {
            "z_score": worst["value"],
            "source_a_id": meta.get("source_a_id"),
            "source_b_id": meta.get("source_b_id"),
            "summary": (
                f"Cross-source spread is "
                f"{abs(float(worst['value'])):.1f} stddev "
                f"{'above' if float(worst['value']) > 0 else 'below'} "
                f"its rolling baseline."
            ),
        }

    result: dict[str, Any] = {
        "slug": price_data["slug"],
        "display_name": price_data["display_name"],
        "tier": tier,
        "per_source": per_source,
        "anomaly_flag": anomaly_flag,
    }

    if tier == "curated" and drift_data is not None:
        result["drift_summary"] = {
            "pairs": [_shape_drift_pair(p) for p in drift_data.get("pairs", [])],
        }
    elif tier == "featured":
        result["tier_note"] = _TIER_NOTE_FEATURED
    elif tier == "substrate":
        active = _find_active_wear(slug)
        result["tier_note"] = _tier_note_substrate(
            active["display_name"] if active else None
        )
        if active is not None:
            result["active_wear_hint"] = active

    return result


def query_drift(slug: str) -> dict:
    """Return the latest drift verdict per Pricempire pair for one
    item.

    Wraps ``/items/{slug}/drift``. Shape:

    .. code-block:: python

        {
            "slug": ...,
            "display_name": ...,
            "tier": "curated" | "featured" | "substrate",
            "pairs": [
                {
                    "source_a": "skinport",
                    "source_b": "pricempire_skinport",
                    "verdict": "no_drift" | ...,
                    "drift_pct": "-1.2%" | None,
                    "framing": "<pre-composed user-facing copy>",
                    "stale_side": "curated" | "pricempire" | "both" | None,
                    "classification": "pattern_agnostic" | ...,
                    "computed_at": "...",
                    "curated_price": "...",
                    "pricempire_price": "...",
                },
                ...
            ],
            "tier_note": "..." | None,           # set for featured/substrate
            "active_wear_hint": {...} | None,    # set when substrate + sibling
        }

    Non-curated tiers receive empty ``pairs`` plus a ``tier_note``. The
    LLM is instructed via the system prompt to render the
    ``framing`` string verbatim per pair, NOT to invent its own
    drift narrative."""
    with _client() as c:
        raw = _get_json(c, f"/items/{slug}/drift")

    tier = raw.get("tier", "curated")
    pairs = [_shape_drift_pair(p) for p in raw.get("pairs", [])]
    result: dict[str, Any] = {
        "slug": raw["slug"],
        "display_name": raw["display_name"],
        "tier": tier,
        "pairs": pairs,
    }
    if tier == "featured":
        result["tier_note"] = _TIER_NOTE_FEATURED
    elif tier == "substrate":
        active = _find_active_wear(slug)
        result["tier_note"] = _tier_note_substrate(
            active["display_name"] if active else None
        )
        if active is not None:
            result["active_wear_hint"] = active
    return result


def query_price_history(
    slug: str,
    source: str | None = None,
    days: int = 7,
    limit: int = 500,
) -> dict:
    """Time-series observations for one item.

    When the API returns more than ``HISTORY_DOWNSAMPLE_THRESHOLD``
    rows (~30), the response is replaced with aggregate per-source
    stats (first/last/min/max/count) instead of the raw observation
    list. This keeps the payload bounded for LLM consumption.
    Below the threshold, the raw response passes through so the LLM
    can cite specific points.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    params: dict[str, Any] = {"since": since, "limit": limit}
    if source is not None:
        params["source"] = source
    with _client() as c:
        raw = _get_json(c, f"/items/{slug}/history", params=params)
    result = _summarize_history(raw)
    tier = raw.get("tier", "curated")
    if tier != "curated":
        result["tier"] = tier
        _attach_tier_envelope(result, slug=slug, tier=tier)
    return result


def render_chart(
    slug: str, source: str = "skinport", days: int = 7
) -> Attachment:
    with _client(timeout_read=60.0) as c:
        try:
            resp = c.get(
                f"/items/{slug}/chart",
                params={"source": source, "days": days},
            )
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach the chart endpoint: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError(
                f"Item or source not found: slug={slug!r}, "
                f"source={source!r}."
            )
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /chart: "
                f"{resp.text[:200]}"
            )
        return Attachment(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/png"),
            filename=f"{slug}-{source}-{days}d.png",
        )


def _attach_tier_envelope(
    result: dict, *, slug: str, tier: str
) -> None:
    """Inject ``tier_note`` and (for substrate + sibling exists)
    ``active_wear_hint`` into the result dict in-place. Centralizes
    the featured/substrate post-processing so every item-level tool stays
    consistent."""
    if tier == "featured":
        result["tier_note"] = _TIER_NOTE_FEATURED
    elif tier == "substrate":
        active = _find_active_wear(slug)
        result["tier_note"] = _tier_note_substrate(
            active["display_name"] if active else None
        )
        if active is not None:
            result["active_wear_hint"] = active


def evaluate_deal(slug: str, amount: str, currency: str) -> dict:
    payload = {
        "slug": slug,
        "offer": {"amount": amount, "currency": currency},
    }
    with _client() as c:
        try:
            resp = c.post("/deals/evaluate", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /deals/evaluate: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError(
                f"Item not on the watchlist: {slug!r}."
            )
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /deals/evaluate: "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        tier = body.get("tier", "curated")
        if tier != "curated":
            _attach_tier_envelope(body, slug=slug, tier=tier)
        return body


def market_baseline_inventory_item(inventory_url: str) -> dict:
    """Return exact inventory attributes plus a market-name USD baseline."""
    payload = {"inventory_url": inventory_url}
    with _client(timeout_read=60.0) as c:
        try:
            resp = c.post("/asset-valuations/inventory", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /asset-valuations/inventory: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /asset-valuations/inventory: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def market_baseline_inventory_summary(inventory_url: str) -> dict:
    """Return a public inventory's summed market-name USD baseline."""
    payload = {"inventory_url": inventory_url}
    with _client(timeout_read=90.0) as c:
        try:
            resp = c.post("/asset-valuations/inventory/summary", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /asset-valuations/inventory/summary: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /asset-valuations/inventory/summary: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def save_portfolio_snapshot(
    inventory_url: str,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Save a summary-level portfolio baseline for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError(
            "Missing Discord user context for portfolio snapshot."
        )
    payload = {
        "discord_user_id": discord_user_id,
        "inventory_url": inventory_url,
    }
    with _client(timeout_read=90.0) as c:
        try:
            resp = c.post("/portfolio/snapshots", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /portfolio/snapshots: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 409:
            raise ApiUnexpectedError(
                f"Portfolio snapshot quota reached: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /portfolio/snapshots: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def list_portfolio_snapshots(
    limit: int = 10,
    steam_id: str | None = None,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """List saved portfolio snapshots for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError(
            "Missing Discord user context for portfolio snapshots."
        )
    params: dict[str, Any] = {
        "discord_user_id": discord_user_id,
        "limit": limit,
    }
    if steam_id is not None:
        params["steam_id"] = steam_id
    with _client() as c:
        raw = _get_json(c, "/portfolio/snapshots", params=params)
    return {"snapshots": raw, "count": len(raw)}


def portfolio_snapshot_trend(
    limit: int = 30,
    steam_id: str | None = None,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Return recent saved portfolio movement for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError(
            "Missing Discord user context for portfolio snapshots."
        )
    params: dict[str, Any] = {
        "discord_user_id": discord_user_id,
        "limit": limit,
    }
    if steam_id is not None:
        params["steam_id"] = steam_id
    with _client() as c:
        return _get_json(c, "/portfolio/snapshots/trend", params=params)


def prune_portfolio_snapshots(
    keep_latest: int = 10,
    steam_id: str | None = None,
    older_than_days: int | None = None,
    dry_run: bool = True,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Preview or delete old portfolio snapshots for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError(
            "Missing Discord user context for portfolio snapshots."
        )
    payload: dict[str, Any] = {
        "discord_user_id": discord_user_id,
        "keep_latest": keep_latest,
        "dry_run": dry_run,
    }
    if steam_id is not None:
        payload["steam_id"] = steam_id
    if older_than_days is not None:
        payload["older_than_days"] = older_than_days
    with _client() as c:
        try:
            resp = c.post("/portfolio/snapshots/prune", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /portfolio/snapshots/prune: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /portfolio/snapshots/prune: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def create_portfolio_monitor(
    inventory_url: str,
    interval_minutes: int = 1440,
    change_threshold_pct: str = "5.00",
    quiet_start_hour: int | None = None,
    quiet_end_hour: int | None = None,
    timezone_offset_minutes: int = 0,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Create a recurring portfolio baseline change monitor."""
    if discord_user_id is None or discord_channel_id is None:
        raise ApiUnexpectedError("Missing Discord context for portfolio monitor.")
    payload = {
        "discord_user_id": discord_user_id,
        "discord_channel_id": discord_channel_id,
        "inventory_url": inventory_url,
        "interval_minutes": interval_minutes,
        "change_threshold_pct": change_threshold_pct,
        "quiet_start_hour": quiet_start_hour,
        "quiet_end_hour": quiet_end_hour,
        "timezone_offset_minutes": timezone_offset_minutes,
    }
    with _client(timeout_read=30.0) as c:
        try:
            resp = c.post("/portfolio/monitors", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /portfolio/monitors: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 409:
            raise ApiUnexpectedError(
                f"Portfolio monitor quota reached: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /portfolio/monitors: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def list_portfolio_monitors(
    include_inactive: bool = False,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """List portfolio monitors for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for portfolio monitors.")
    with _client() as c:
        raw = _get_json(
            c,
            "/portfolio/monitors",
            params={
                "discord_user_id": discord_user_id,
                "include_inactive": include_inactive,
            },
        )
    return {"monitors": raw, "count": len(raw)}


def cancel_portfolio_monitor(
    monitor_id: str,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Cancel one portfolio monitor owned by the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for portfolio monitors.")
    payload = {"discord_user_id": discord_user_id}
    with _client() as c:
        try:
            resp = c.post(f"/portfolio/monitors/{monitor_id}/cancel", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /portfolio/monitors/{monitor_id}/cancel: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError("Portfolio monitor not found.")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /portfolio/monitors/{monitor_id}/cancel: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def market_baseline_inspect_link(inspect_url: str) -> dict:
    """Return decoded inspect attributes plus a market-name USD baseline."""
    payload = {"inspect_url": inspect_url}
    with _client(timeout_read=60.0) as c:
        try:
            resp = c.post("/asset-valuations/inspect", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /asset-valuations/inspect: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /asset-valuations/inspect: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def create_price_alert(
    slug: str,
    direction: str,
    threshold_price: str | None = None,
    currency: str = "usd",
    alert_mode: str = "price_threshold",
    threshold_pct: str | None = None,
    quiet_start_hour: int | None = None,
    quiet_end_hour: int | None = None,
    timezone_offset_minutes: int = 0,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Create one persistent Discord-owned price alert."""
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for price alert.")
    payload = {
        "discord_user_id": discord_user_id,
        "discord_channel_id": discord_channel_id,
        "slug": slug,
        "alert_mode": alert_mode,
        "direction": direction,
        "threshold_price": threshold_price,
        "threshold_pct": threshold_pct,
        "currency": currency,
        "quiet_start_hour": quiet_start_hour,
        "quiet_end_hour": quiet_end_hour,
        "timezone_offset_minutes": timezone_offset_minutes,
    }
    with _client() as c:
        try:
            resp = c.post("/alerts/price", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /alerts/price: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError(
                f"Item not found for alert: {slug!r}."
            )
        if resp.status_code == 409:
            raise ApiUnexpectedError(
                f"Price alert quota reached: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /alerts/price: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def list_price_alerts(
    include_inactive: bool = False,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """List persistent price alerts for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for price alerts.")
    with _client() as c:
        raw = _get_json(
            c,
            "/alerts/price",
            params={
                "discord_user_id": discord_user_id,
                "include_inactive": include_inactive,
            },
        )
    return {"alerts": raw, "count": len(raw)}


def cancel_price_alert(
    alert_id: str,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Cancel one persistent price alert owned by the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for price alerts.")
    payload = {"discord_user_id": discord_user_id}
    with _client() as c:
        try:
            resp = c.post(f"/alerts/price/{alert_id}/cancel", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /alerts/price/{alert_id}/cancel: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError("Alert not found for your Discord user.")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /alerts/price/{alert_id}/cancel: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def evaluate_triggered_price_alerts(limit: int = 100) -> dict:
    """Evaluate active price alerts and return newly triggered rows."""
    with _client(timeout_read=30.0) as c:
        try:
            resp = c.post("/alerts/price/evaluate", json={"limit": limit})
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /alerts/price/evaluate: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /alerts/price/evaluate: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def mark_price_alert_delivery(
    alert_id: str,
    delivered: bool,
    error: str | None = None,
) -> dict:
    """Record whether Discord delivery succeeded for one triggered alert."""
    payload = {"delivered": delivered, "error": error}
    with _client(timeout_read=30.0) as c:
        try:
            resp = c.post(f"/alerts/price/{alert_id}/delivery", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /alerts/price/{alert_id}/delivery: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError("Alert not found for delivery update.")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /alerts/price/{alert_id}/delivery: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def narrative_today() -> dict:
    """Latest daily narrative. The ``text`` field is bounded (one
    English paragraph) and passes through unchanged; the ``meta``
    citation block can be verbose (lists of top-movers / anomalies /
    divergences) and is replaced with a compact ``{as_of,
    cited_count}`` shape — the LLM doesn't need the citation rows to
    render the paragraph for the user."""
    with _client() as c:
        raw = _get_json(c, "/insights/narrative/latest")
    return _trim_narrative_meta(raw)


def whats_interesting(hours: int = 6) -> dict:
    """Currently-firing anomalies. When the API returns more than
    ``ANOMALIES_TOP_N_THRESHOLD`` rows, only the top-N by ``|z|`` are
    returned, plus a ``total_count`` so the LLM can mention how many
    were elided. Below the threshold, the raw shape passes through."""
    with _client() as c:
        raw = _get_json(
            c, "/insights/anomalies/recent", params={"hours": hours}
        )
    return _summarize_anomalies(raw)


def market_signal_digest(
    hours: int = 6,
    limit: int = 8,
    lane: str = "all",
) -> dict:
    """Ranked compact anomaly digest for Discord rendering."""
    with _client() as c:
        return _get_json(
            c,
            "/insights/signals/digest",
            params={"lane": lane, "hours": hours, "limit": limit},
        )


def create_signal_subscription(
    lane: str = "all",
    hours: int = 6,
    limit: int = 8,
    threshold_z: str = "3.00",
    interval_minutes: int = 360,
    quiet_start_hour: int | None = None,
    quiet_end_hour: int | None = None,
    timezone_offset_minutes: int = 0,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Create a recurring signal digest subscription for the current channel."""
    if discord_user_id is None or discord_channel_id is None:
        raise ApiUnexpectedError("Missing Discord context for signal subscription.")
    payload = {
        "discord_user_id": discord_user_id,
        "discord_channel_id": discord_channel_id,
        "lane": lane,
        "hours": hours,
        "limit": limit,
        "threshold_z": threshold_z,
        "interval_minutes": interval_minutes,
        "quiet_start_hour": quiet_start_hour,
        "quiet_end_hour": quiet_end_hour,
        "timezone_offset_minutes": timezone_offset_minutes,
    }
    with _client() as c:
        try:
            resp = c.post("/signals/subscriptions", json=payload)
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /signals/subscriptions: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 409:
            raise ApiUnexpectedError(
                f"Signal subscription quota reached: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /signals/subscriptions: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def list_signal_subscriptions(
    include_inactive: bool = False,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """List signal digest subscriptions for the current Discord user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for signal digests.")
    with _client() as c:
        raw = _get_json(
            c,
            "/signals/subscriptions",
            params={
                "discord_user_id": discord_user_id,
                "include_inactive": include_inactive,
            },
        )
    return {"subscriptions": raw, "count": len(raw)}


def cancel_signal_subscription(
    subscription_id: str,
    discord_user_id: str | None = None,
    discord_channel_id: str | None = None,
) -> dict:
    """Cancel one signal digest subscription owned by the current user."""
    del discord_channel_id
    if discord_user_id is None:
        raise ApiUnexpectedError("Missing Discord user context for signal digests.")
    payload = {"discord_user_id": discord_user_id}
    with _client() as c:
        try:
            resp = c.post(
                f"/signals/subscriptions/{subscription_id}/cancel",
                json=payload,
            )
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                "Couldn't reach "
                f"/signals/subscriptions/{subscription_id}/cancel: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError("Signal subscription not found.")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /signals/subscriptions/"
                f"{subscription_id}/cancel: {resp.text[:200]}"
            )
        return resp.json()


def evaluate_signal_subscriptions(limit: int = 100) -> dict:
    """Evaluate due signal digest subscriptions for delivery."""
    with _client(timeout_read=30.0) as c:
        try:
            resp = c.post("/signals/subscriptions/evaluate", json={"limit": limit})
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /signals/subscriptions/evaluate: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /signals/subscriptions/evaluate: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def mark_signal_subscription_delivery(
    subscription_id: str,
    delivered: bool,
    digest_fingerprint: str | None = None,
    error: str | None = None,
) -> dict:
    """Record Discord delivery state for one signal digest subscription."""
    payload = {
        "delivered": delivered,
        "digest_fingerprint": digest_fingerprint,
        "error": error,
    }
    with _client(timeout_read=30.0) as c:
        try:
            resp = c.post(
                f"/signals/subscriptions/{subscription_id}/delivery",
                json=payload,
            )
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                "Couldn't reach "
                f"/signals/subscriptions/{subscription_id}/delivery: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError("Signal subscription not found.")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /signals/subscriptions/"
                f"{subscription_id}/delivery: {resp.text[:200]}"
            )
        return resp.json()


def evaluate_portfolio_monitors(limit: int = 25) -> dict:
    """Evaluate due portfolio monitors and return delivery payloads."""
    with _client(timeout_read=180.0) as c:
        try:
            resp = c.post("/portfolio/monitors/evaluate", json={"limit": limit})
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /portfolio/monitors/evaluate: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                f"Unexpected {resp.status_code} from /portfolio/monitors/evaluate: "
                f"{resp.text[:200]}"
            )
        return resp.json()


def mark_portfolio_monitor_delivery(
    monitor_id: str,
    delivered: bool,
    snapshot_id: str | None = None,
    error: str | None = None,
) -> dict:
    """Record Discord delivery state for one portfolio monitor."""
    payload = {"delivered": delivered, "snapshot_id": snapshot_id, "error": error}
    with _client(timeout_read=30.0) as c:
        try:
            resp = c.post(
                f"/portfolio/monitors/{monitor_id}/delivery",
                json=payload,
            )
        except httpx.RequestError as exc:
            raise ApiUnreachableError(
                f"Couldn't reach /portfolio/monitors/{monitor_id}/delivery: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise ApiAuthError("API rejected the bearer token (401).")
        if resp.status_code == 404:
            raise ItemNotInWatchlistError("Portfolio monitor not found.")
        if resp.status_code >= 400:
            raise ApiUnexpectedError(
                "Unexpected "
                f"{resp.status_code} from /portfolio/monitors/{monitor_id}/delivery: "
                f"{resp.text[:200]}"
            )
        return resp.json()


# Heuristic CS2 weapon → category mapping. Order matters:
#
# - **Gloves first** — both knife and glove items use the ``★`` prefix
#   ("★ Karambit ..." vs "★ Sport Gloves ..."), so a knife-first
#   ordering would mis-categorize all gloves. The "Gloves" / "Hand
#   Wraps" tokens are unambiguous and let glove items short-circuit.
# - Then knife — ``★`` + ``Knife`` + specific knife names.
# - Then weapon families.
#
# Items not matching any pattern go into ``other``; mis-categorization
# costs nothing (it's only used for a count summary).
_CATEGORY_PATTERNS: list[tuple[str, list[str]]] = [
    ("gloves", ["Gloves", "Hand Wraps"]),
    (
        "knife",
        ["★", "Knife", "Bayonet", "Karambit", "Daggers"],
    ),
    (
        "rifle",
        ["AK-47", "M4A4", "M4A1-S", "Galil AR", "FAMAS", "AUG", "SG 553"],
    ),
    ("sniper", ["AWP", "SSG 08", "G3SG1", "SCAR-20"]),
    (
        "pistol",
        [
            "Desert Eagle",
            "USP-S",
            "Glock-18",
            "Five-SeveN",
            "Tec-9",
            "CZ75-Auto",
            "P250",
            "P2000",
            "R8 Revolver",
            "Dual Berettas",
        ],
    ),
    (
        "smg",
        ["MP9", "MP7", "MP5", "MAC-10", "UMP-45", "PP-Bizon", "P90"],
    ),
    ("shotgun", ["Nova", "XM1014", "Sawed-Off", "MAG-7"]),
    ("lmg", ["M249", "Negev"]),
]


def _category(item_name: str) -> str:
    """Best-guess CS2 weapon category from the display_name string."""
    for cat, patterns in _CATEGORY_PATTERNS:
        for p in patterns:
            if p in item_name:
                return cat
    return "other"


def _summarize_watchlist(items: list[dict]) -> dict:
    """Replace the raw 48-item list with ``{count, by_category,
    sample}``. by_category is sorted desc by count so the largest
    category shows first when the LLM renders."""
    by_cat: dict[str, int] = {}
    for it in items:
        c = _category(it.get("display_name", ""))
        by_cat[c] = by_cat.get(c, 0) + 1
    return {
        "count": len(items),
        "by_category": dict(
            sorted(by_cat.items(), key=lambda x: -x[1])
        ),
        "sample": [
            {
                "slug": it.get("slug"),
                "display_name": it.get("display_name"),
            }
            for it in items[:WATCHLIST_SAMPLE_SIZE]
        ],
    }


def _summarize_history(raw: dict) -> dict:
    """Pass through small results; aggregate large ones.

    Aggregation shape: ``{slug, source, since, until, count,
    downsampled=True, per_source_stats: {source: {denomination,
    count, first_price, first_observed, last_price, last_observed,
    min_price, max_price}}}``. The LLM has enough to answer "how has
    X moved?" without seeing each individual observation.
    """
    obs = raw.get("observations") or []
    if len(obs) <= HISTORY_DOWNSAMPLE_THRESHOLD:
        return raw

    by_source: dict[str, list[dict]] = {}
    for o in obs:
        by_source.setdefault(o.get("source", "?"), []).append(o)

    per_source_stats: dict[str, dict] = {}
    for s, src_obs in by_source.items():
        # Sort newest-first → oldest-first so first/last is
        # chronological. The API returns timestamp DESC; we reverse.
        src_obs_chrono = sorted(
            src_obs, key=lambda o: o.get("timestamp", "")
        )
        prices: list[float] = []
        for o in src_obs_chrono:
            with contextlib.suppress(TypeError, ValueError):
                prices.append(float(o.get("price", "0")))
        if not prices:
            continue
        first = src_obs_chrono[0]
        last = src_obs_chrono[-1]
        per_source_stats[s] = {
            "denomination": first.get("denomination"),
            "count": len(prices),
            "first_price": first.get("price"),
            "first_observed": first.get("timestamp"),
            "last_price": last.get("price"),
            "last_observed": last.get("timestamp"),
            "min_price": f"{min(prices):.2f}",
            "max_price": f"{max(prices):.2f}",
        }

    return {
        "slug": raw.get("slug"),
        "source": raw.get("source"),
        "since": raw.get("since"),
        "until": raw.get("until"),
        "count": raw.get("count", len(obs)),
        "downsampled": True,
        "downsample_note": (
            f"{raw.get('count', len(obs))} raw observations summarized "
            f"into per-source aggregates "
            f"(threshold={HISTORY_DOWNSAMPLE_THRESHOLD})."
        ),
        "per_source_stats": per_source_stats,
    }


def _summarize_anomalies(raw: dict) -> dict:
    """Pass through ≤10 anomalies; otherwise return the top-N by
    absolute z-score + a ``total_count`` so the LLM can say "X
    anomalies total; here are the most severe"."""
    anomalies = raw.get("anomalies") or []
    if len(anomalies) <= ANOMALIES_TOP_N_THRESHOLD:
        return raw

    def _abs_z(a: dict) -> float:
        try:
            return abs(float(a.get("z_score", "0") or "0"))
        except (TypeError, ValueError):
            return 0.0

    top = sorted(anomalies, key=_abs_z, reverse=True)[
        :ANOMALIES_TOP_N_THRESHOLD
    ]
    return {
        "since": raw.get("since"),
        "total_count": len(anomalies),
        "downsampled": True,
        "downsample_note": (
            f"{len(anomalies)} anomalies total; top "
            f"{ANOMALIES_TOP_N_THRESHOLD} by |z-score| returned."
        ),
        "anomalies": top,
    }


def _trim_narrative_meta(raw: dict) -> dict:
    """Keep ``text`` as-is; collapse the citation ``meta`` block into
    ``{as_of, cited_count}``. The LLM renders the paragraph text;
    it doesn't need the citation rows to do so."""
    meta = raw.get("meta") or {}
    cited_count = (
        len(meta.get("top_movers") or [])
        + len(meta.get("volume_anomalies") or [])
        + len(meta.get("cross_source_divergences") or [])
    )
    return {
        "computed_at": raw.get("computed_at"),
        "text": raw.get("text"),
        "meta": {
            "as_of": meta.get("as_of"),
            "cited_count": cited_count,
        },
    }


# ---------------------------------------------------------------------
# Tool declarations + dispatch table
# ---------------------------------------------------------------------


# DeepSeek's chat API expects tools in OpenAI-compatible JSON-schema
# shape. The description fields here are read by the LLM at every
# turn to decide which tool to call; they need concrete trigger
# examples because open-source models are less reliable at intent
# inference than cloud-tier models (ADR 016 §"Defensive handling").
TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_watchlist",
            "description": (
                "Call this for watchlist/list/what-do-you-track questions. "
                "Returns a summarized watchlist. Do not use for named item "
                "questions with explicit or omitted wear."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_current_price",
            "description": (
                "Call this for specific item price, how-much, or up/down "
                "questions. Returns the current per-source price snapshot. "
                "Curated items may include drift_summary and anomaly_flag."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Lowercase hyphenated item slug, e.g. "
                            "ak-47-redline-field-tested."
                        ),
                    }
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_price_history",
            "description": (
                "Call this for movement, history, trend, or this-week "
                "questions. Returns a price time series for one item."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": (
                            "Optional: skinport, dmarket, or steam_market."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": "Lookback window. Default 7.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_chart",
            "description": (
                "Call this for chart, plot, graph, or visualize requests. "
                "Generates a PNG chart for one source over N days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": (
                            "Source to plot: skinport, dmarket, or steam_market."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": "Window in days. Default 7.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_deal",
            "description": (
                "Call this for fair, good price, should I pay, or worth-it "
                "questions. Evaluates one item and offered amount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "amount": {
                        "type": "string",
                        "description": (
                            "Decimal as string, e.g. 42.50. Do not pass float."
                        ),
                    },
                    "currency": {
                        "type": "string",
                        "enum": ["usd", "wallet_credit"],
                        "description": (
                            "usd for dollars; wallet_credit for Steam wallet SC."
                        ),
                    },
                },
                "required": ["slug", "amount", "currency"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "narrative_today",
            "description": (
                "Call this for today, daily summary, market recap, or what's "
                "new. Returns the latest daily market summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_price_alert",
            "description": (
                "Call this when the user asks to alert, notify, or remind "
                "them when a tracked item's price reaches a threshold or "
                "moves by a requested percent from the current price. "
                "Use at_or_below for buy/drop alerts and at_or_above for "
                "rise/sell alerts. Use alert_mode=percent_move plus "
                "threshold_pct for percent drop/rise requests. Discord "
                "user/channel context is injected by the bot; do not ask "
                "the user for IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Tracked item slug.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["at_or_below", "at_or_above"],
                    },
                    "alert_mode": {
                        "type": "string",
                        "enum": ["price_threshold", "percent_move"],
                        "description": (
                            "Default price_threshold; use percent_move for "
                            "percent drop/rise alerts."
                        ),
                    },
                    "threshold_price": {
                        "type": "string",
                        "description": "Decimal price threshold, e.g. 25.50.",
                    },
                    "threshold_pct": {
                        "type": "string",
                        "description": "Percent move threshold, e.g. 10.00.",
                    },
                    "currency": {
                        "type": "string",
                        "enum": ["usd", "wallet_credit"],
                        "description": "usd for dollars; wallet_credit for SC.",
                    },
                    "quiet_start_hour": {
                        "type": "integer",
                        "description": "Optional local quiet start hour, 0-23.",
                    },
                    "quiet_end_hour": {
                        "type": "integer",
                        "description": "Optional local quiet end hour, 0-23.",
                    },
                    "timezone_offset_minutes": {
                        "type": "integer",
                        "description": "User local offset from UTC in minutes.",
                    },
                },
                "required": ["slug", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_price_alerts",
            "description": (
                "Call this when the user asks what price alerts they have "
                "set or wants to list active alerts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_inactive": {
                        "type": "boolean",
                        "description": "Include triggered/cancelled alerts when true.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_price_alert",
            "description": (
                "Call this when the user asks to delete, cancel, or remove "
                "one of their price alerts by alert id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert id returned by list_price_alerts.",
                    }
                },
                "required": ["alert_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_baseline_inventory_item",
            "description": (
                "Call this when the user pastes a Steam public inventory "
                "item link or asks about float, seed, stickers, or a "
                "market baseline for an exact inventory asset. Returns "
                "exact float/seed/stickers plus a market-name USD baseline "
                "and a deterministic premium-evidence section when local "
                "market data exists. It does not price float, seed, sticker, "
                "or charm premiums."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inventory_url": {
                        "type": "string",
                        "description": (
                            "Full steamcommunity.com inventory item URL, "
                            "including #730_2_<asset_id>."
                        ),
                    }
                },
                "required": ["inventory_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_baseline_inventory_summary",
            "description": (
                "Call this when the user asks for total inventory value, "
                "portfolio value, top inventory items, or a public Steam "
                "inventory summary. Returns a summed market-name USD baseline "
                "for priced CS2 inventory assets plus top items and a "
                "deterministic premium-evidence summary. It does not price "
                "float, seed, sticker, or charm premiums."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inventory_url": {
                        "type": "string",
                        "description": (
                            "Steam inventory URL, optionally with a "
                            "#730_2_<asset_id> fragment."
                        ),
                    }
                },
                "required": ["inventory_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_portfolio_snapshot",
            "description": (
                "Call this when the user wants to save, track, snapshot, or "
                "record their public Steam inventory portfolio baseline. "
                "Discord user context is injected by the bot; do not ask "
                "the user for IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inventory_url": {
                        "type": "string",
                        "description": "Public Steam inventory URL.",
                    }
                },
                "required": ["inventory_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_portfolio_snapshots",
            "description": (
                "Call this when the user asks to list saved portfolio "
                "snapshots or recent inventory baseline records."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum snapshots to return. Default 10.",
                    },
                    "steam_id": {
                        "type": "string",
                        "description": "Optional SteamID64 filter.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "portfolio_snapshot_trend",
            "description": (
                "Call this when the user asks how their saved portfolio is "
                "doing, how it changed, P/L, trend, or movement since the "
                "last snapshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Recent snapshot window. Default 30.",
                    },
                    "steam_id": {
                        "type": "string",
                        "description": "Optional SteamID64 filter.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prune_portfolio_snapshots",
            "description": (
                "Call this when the user asks to delete, prune, clean up, "
                "or preview deletion of saved portfolio snapshots. Discord "
                "user context is injected by the bot; do not ask the user "
                "for IDs. Use dry_run=true for preview requests and "
                "dry_run=false only when the user explicitly asks to delete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_latest": {
                        "type": "integer",
                        "description": "Number of newest snapshots to keep. Default 10.",
                    },
                    "steam_id": {
                        "type": "string",
                        "description": "Optional SteamID64 filter.",
                    },
                    "older_than_days": {
                        "type": "integer",
                        "description": "Only prune snapshots older than this many days.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview only when true. Default true.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_portfolio_monitor",
            "description": (
                "Call this when the user asks to monitor, subscribe to, or "
                "alert on changes in a public Steam inventory portfolio "
                "baseline over time. Discord user/channel context is injected "
                "by the bot; do not ask the user for IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inventory_url": {
                        "type": "string",
                        "description": "Public Steam inventory URL.",
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Check interval in minutes. Default 1440.",
                    },
                    "change_threshold_pct": {
                        "type": "string",
                        "description": "Mid-baseline movement threshold. Default 5.00.",
                    },
                    "quiet_start_hour": {
                        "type": "integer",
                        "description": "Optional local quiet start hour, 0-23.",
                    },
                    "quiet_end_hour": {
                        "type": "integer",
                        "description": "Optional local quiet end hour, 0-23.",
                    },
                    "timezone_offset_minutes": {
                        "type": "integer",
                        "description": "User local offset from UTC in minutes.",
                    },
                },
                "required": ["inventory_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_portfolio_monitors",
            "description": (
                "Call this when the user asks to list portfolio monitors or "
                "inventory change subscriptions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_inactive": {
                        "type": "boolean",
                        "description": "Include cancelled monitors when true.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_portfolio_monitor",
            "description": (
                "Call this when the user asks to cancel, remove, or stop a "
                "portfolio monitor by monitor id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor_id": {
                        "type": "string",
                        "description": "Monitor id from list_portfolio_monitors.",
                    }
                },
                "required": ["monitor_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_baseline_inspect_link",
            "description": (
                "Call this when the user pastes a CS2 inspect link, "
                "steam://run link, or asks about float, seed, stickers, "
                "or a market baseline for an exact inspect asset. Returns "
                "decoded float/seed/stickers plus a market-name USD "
                "baseline and a deterministic premium-evidence section when "
                "local market data exists. It does not price float, seed, "
                "sticker, or charm premiums."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inspect_url": {
                        "type": "string",
                        "description": (
                            "Full CS2 inspect URL, usually starting with "
                            "steam://run/730 or steam://rungame/730."
                        ),
                    }
                },
                "required": ["inspect_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_drift",
            "description": (
                "Call this for drift, Pricempire consistency, or "
                "source-agreement checks. Returns latest Pricempire drift "
                "verdicts for one item. Render pair framing verbatim."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Lowercase hyphenated item slug."
                        ),
                    }
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whats_interesting",
            "description": (
                "Call this for interesting, moving, anomalies, or weird-today "
                "questions. Returns current cross-source spread and volume "
                "anomalies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": (
                            "Lookback in hours. Default 6, max 24."
                        ),
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "market_signal_digest",
            "description": (
                "Call this when the user asks what to watch, asks for a "
                "market signal digest, market movers, spread watch, or the "
                "highest-priority opportunities right now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lane": {
                        "type": "string",
                        "enum": ["all", "market_movers", "spread_watch"],
                        "description": (
                            "Signal lane. Use market_movers for volume/momentum "
                            "watch, spread_watch for cross-source spreads, all "
                            "for the broad digest."
                        ),
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Lookback in hours. Default 6, max 24.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum signals to return. Default 8.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_signal_subscription",
            "description": (
                "Call this when the user asks to subscribe a channel to "
                "recurring market signal digests, market movers, or spread "
                "watch updates. Discord user/channel context is injected by "
                "the bot; do not ask the user for IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lane": {
                        "type": "string",
                        "enum": ["all", "market_movers", "spread_watch"],
                        "description": (
                            "Signal lane. Use market_movers for volume/momentum "
                            "watch, spread_watch for cross-source spreads, all "
                            "for the broad digest."
                        ),
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Lookback in hours. Default 6, max 24.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum signals per digest. Default 8.",
                    },
                    "threshold_z": {
                        "type": "string",
                        "description": "Minimum absolute z-score. Default 3.00.",
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Delivery interval in minutes. Default 360.",
                    },
                    "quiet_start_hour": {
                        "type": "integer",
                        "description": "Optional local quiet start hour, 0-23.",
                    },
                    "quiet_end_hour": {
                        "type": "integer",
                        "description": "Optional local quiet end hour, 0-23.",
                    },
                    "timezone_offset_minutes": {
                        "type": "integer",
                        "description": "User local offset from UTC in minutes.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_signal_subscriptions",
            "description": (
                "Call this when the user asks to list recurring signal digest "
                "subscriptions or market-mover channel subscriptions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_inactive": {
                        "type": "boolean",
                        "description": "Include cancelled subscriptions when true.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_signal_subscription",
            "description": (
                "Call this when the user asks to cancel, remove, or stop a "
                "signal digest subscription by subscription id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "description": "Subscription id from list_signal_subscriptions.",
                    }
                },
                "required": ["subscription_id"],
            },
        },
    },
]


TOOL_FUNCTIONS: dict[str, Any] = {
    "list_watchlist": list_watchlist,
    "query_current_price": query_current_price,
    "query_price_history": query_price_history,
    "render_chart": render_chart,
    "evaluate_deal": evaluate_deal,
    "market_baseline_inventory_item": market_baseline_inventory_item,
    "market_baseline_inventory_summary": market_baseline_inventory_summary,
    "save_portfolio_snapshot": save_portfolio_snapshot,
    "list_portfolio_snapshots": list_portfolio_snapshots,
    "portfolio_snapshot_trend": portfolio_snapshot_trend,
    "prune_portfolio_snapshots": prune_portfolio_snapshots,
    "create_portfolio_monitor": create_portfolio_monitor,
    "list_portfolio_monitors": list_portfolio_monitors,
    "cancel_portfolio_monitor": cancel_portfolio_monitor,
    "market_baseline_inspect_link": market_baseline_inspect_link,
    "create_price_alert": create_price_alert,
    "list_price_alerts": list_price_alerts,
    "cancel_price_alert": cancel_price_alert,
    "query_drift": query_drift,
    "narrative_today": narrative_today,
    "whats_interesting": whats_interesting,
    "market_signal_digest": market_signal_digest,
    "create_signal_subscription": create_signal_subscription,
    "list_signal_subscriptions": list_signal_subscriptions,
    "cancel_signal_subscription": cancel_signal_subscription,
}


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
