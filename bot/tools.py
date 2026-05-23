"""Skin-market API tool wrappers — Phase 7c.

Seven Python functions that wrap HTTP calls against the local read
API (``http://api:8000`` from inside compose, ``http://localhost:8001``
from the host). The Discord bot's LLM router decides which to call;
``bot.deepseek_client`` executes the call and feeds the result back.

The function bodies + typed exception hierarchy + three-state composer
for ``query_current_price`` are carried forward from the Phase 7b
Hermes attempt (now archived at
``docs/archive/bot_skill_hermes_attempt/``). What changed in 7c:

- The ``@tool`` decorator and ``TOOLS`` list are gone. Tools are
  declared as OpenAI-compatible JSON-schema dicts in
  ``TOOL_DEFINITIONS`` and a parallel ``TOOL_FUNCTIONS`` dict maps
  ``name → callable`` for the executor to dispatch by name.
- Tool function bodies stay synchronous; the bot wraps each call in
  ``asyncio.to_thread`` so a slow API call doesn't block the
  discord.py event loop.

Phase 7c-fix added tool-result size discipline (ADR 016 §11) — the
``_summarize_*`` helpers cap what gets fed to the LLM so it doesn't
spend wall-clock time rendering unbounded structured data.

ADR 016 documents the broader runtime design and the open-source
tool-calling defensive posture.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Inside docker-compose the bot reaches the api over the internal
# network at its service name + container port. The host's
# 127.0.0.1:8001 mapping (Phase 6.6) is only relevant when running
# the bot outside compose for testing.
DEFAULT_API_BASE_URL = "http://api:8000"

EXPECTED_SOURCES: tuple[str, ...] = ("skinport", "dmarket", "steam_market")

_DENOMINATION_BY_SOURCE: dict[str, str] = {
    "steam_market": "wallet_credit",
    "skinport": "usd",
    "dmarket": "usd",
}

STALE_HOURS: int = 4
ANOMALY_FRESHNESS_HOURS: int = 2

# Phase 7c-fix — tool-result size discipline. Open-source LLMs spend
# real wall-clock time rendering structured data; a 48-item list
# took the bot past its old local-model timeout in live testing. Cap
# what the LLM sees per tool. ADR 016 §"Tool result size discipline"
# documents the constraint as load-bearing.
#
# Above these row counts, tool functions return a summarized shape
# (aggregate stats + a few representative rows) instead of the raw
# list. Below the threshold, the raw shape passes through unchanged.
HISTORY_DOWNSAMPLE_THRESHOLD: int = 30
ANOMALIES_TOP_N_THRESHOLD: int = 10
WATCHLIST_SAMPLE_SIZE: int = 5

# Phase 2b Step 9 — tier-aware response shaping. Pre-composed copy
# the LLM renders verbatim when an item is featured-tier or
# substrate; avoids relying on the open-source model to invent the
# right framing on its own (ADR 016's defensive-handling rationale).
# (Tier vocabulary renamed deep/broad/orphan → curated/featured/
# substrate at Phase 2c, ADR 024.)
_TIER_NOTE_FEATURED: str = (
    "This item is on the featured watchlist — we track it but with "
    "less detail than our priority (curated) items. Detailed drift "
    "checks aren't available for this tier."
)


def _tier_note_substrate(active_wear_display_name: str | None) -> str:
    """Compose the substrate tier_note. When an actively-tracked
    sibling wear exists, the note points the user to it.

    Substrate covers two semantic subtypes that share the same
    rendering:
    - Previously-curated wears that got dropped from the YAML at a
      re-seed (Phase 2b Step 7.1 dropped 28 from the prior 48-item
      curated set; 25 of those were re_added to featured at Phase 2c,
      3 remain substrate).
    - Never-curated catalog items present in the items table from a
      Phase 2c bulk-seed (when Path A is live).
    The envelope copy stays generic; the bot doesn't distinguish the
    two subtypes today. Phase 3+ on-demand fetch will, when needed.
    """
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


# Mapping from drift verdict → user-facing framing string + whether
# the bot should render a drift number. See Step 9 design proposal §1
# for the precedence table and analytics/drift.py for verdict
# semantics.
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


# ---------------------------------------------------------------------
# Typed exceptions — the bot catches these in the tool-execution loop
# and feeds str(exc) back to the LLM as the tool_result so the model
# can render a graceful user-facing reply.
# ---------------------------------------------------------------------


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


# ---------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------


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
            "API rejected the bearer token (401). The operator "
            "needs to verify SKIN_MARKET_API_TOKEN."
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


# ---------------------------------------------------------------------
# Items cache + sibling-wear matcher (Phase 2b Step 9; Phase 2c
# rename: orphan→substrate, deep→curated)
#
# The bot needs to know "which wear of skin X is the currently-active
# (curated-tier) one" when a user lands on a substrate slug — both
# for the active_wear_hint surfaced through query_current_price /
# query_drift / evaluate_deal, and for the system-prompt wear-
# disambiguation rule.
#
# The items list grows under Path A (Phase 2c bulk-seed → ~5,000
# items), but only changes at deploy time (data/watchlist.yaml is
# operator-edited and seed_catalog.py is operator-invoked). One
# module-level cache for the lifetime of the bot process is
# sufficient; ``_refresh_items_cache`` allows tests to inject a
# fixture without reaching the API.
# ---------------------------------------------------------------------


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

    Match uses the structured fields on /items rows (added in Phase
    2b Step 9: weapon_name, skin_name, is_stattrak, is_souvenir);
    parsing display_name strings would be brittle on StatTrak™ /
    Souvenir / star-prefixed knives and gloves."""
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


# ---------------------------------------------------------------------
# Drift summary helper (Phase 2b Step 9)
#
# Translates a /items/{slug}/drift response's pair list into the
# ``drift_summary`` block consumed by both query_current_price (where
# it appears alongside per_source + anomaly_flag) and query_drift
# (where it's the primary payload). Centralizes the framing-string
# composition so the two tool surfaces stay in sync.
# ---------------------------------------------------------------------


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
    the stale_* verdicts (so the LLM doesn't have to parse the
    verdict string)."""
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


# ---------------------------------------------------------------------
# Tool function bodies
# ---------------------------------------------------------------------


def list_watchlist() -> dict:
    """Return a **summarized** view of the watchlist for LLM
    consumption: ``{count, by_category, sample}``. The raw 48-item
    list with full per-item fields blows past Qwen3 27b's
    practical rendering latency (>120s in live testing); the
    summarization is part of the bot's load-bearing size-discipline
    contract (ADR 016 §"Tool result size discipline").

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

    Phase 2b Step 9 adds three optional response keys:

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
            # Only curated-tier items get drift detection (analytics/drift.py
            # filters on tier=curated before evaluating). Skipping the
            # /drift call for non-curated tiers avoids one HTTP
            # round-trip
            # per query and keeps the response shape consistent.
            drift_data = _get_json(c, f"/items/{slug}/drift")

    fresh_by_source: dict[str, dict] = {
        s["source"]: s for s in price_data["sources"]
    }
    divergence_rows: list[dict] = []

    # item_unavailability_streak was removed in Phase 2c (2026-05-18);
    # see TODO.md. Sources without a recent observation fall through
    # to the "never_observed" rendering below — the streak-based
    # "unavailable for N cycles" branch no longer exists.

    now = datetime.now(UTC)
    for insight in insights_data["insights"]:
        meta = insight.get("meta") or {}
        if insight["insight_type"] == "cross_source_divergence":
            computed_at = _parse_iso(insight["computed_at"])
            age_h = (now - computed_at).total_seconds() / 3600
            if age_h > ANOMALY_FRESHNESS_HOURS:
                continue
            # Coexistence rule (Step 9 design proposal §1):
            # cross_source_divergence rows involving a Pricempire
            # sub-provider are suppressed here in favor of the
            # drift_summary rendering. Today this is empty by
            # construction (cross_source_divergence is curated-only);
            # the filter is defense-in-depth.
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
            # Three-state availability collapsed to two in Phase 2c
            # (2026-05-18) with the item_unavailability_streak removal:
            # the bot used to surface a streak-counted "unavailable"
            # state here. Sources without observations now render as
            # never_observed uniformly.
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
    item. Phase 2b Step 9.

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


# ---------------------------------------------------------------------
# Size-discipline summarizers (Phase 7c-fix)
# ---------------------------------------------------------------------

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
            "name": "market_baseline_inventory_item",
            "description": (
                "Call this when the user pastes a Steam public inventory "
                "item link or asks about float, seed, stickers, or a "
                "market baseline for an exact inventory asset. Returns "
                "exact float/seed/stickers plus a market-name USD baseline "
                "when local market data exists. It does not price float, "
                "seed, sticker, or charm premiums."
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
                "for priced CS2 inventory assets plus top items. It does not "
                "price float, seed, sticker, or charm premiums."
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
            "name": "market_baseline_inspect_link",
            "description": (
                "Call this when the user pastes a CS2 inspect link, "
                "steam://run link, or asks about float, seed, stickers, "
                "or a market baseline for an exact inspect asset. Returns "
                "decoded float/seed/stickers plus a market-name USD "
                "baseline when local market data exists. It does not price "
                "float, seed, sticker, or charm premiums."
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
]


TOOL_FUNCTIONS: dict[str, Any] = {
    "list_watchlist": list_watchlist,
    "query_current_price": query_current_price,
    "query_price_history": query_price_history,
    "render_chart": render_chart,
    "evaluate_deal": evaluate_deal,
    "market_baseline_inventory_item": market_baseline_inventory_item,
    "market_baseline_inventory_summary": market_baseline_inventory_summary,
    "market_baseline_inspect_link": market_baseline_inspect_link,
    "query_drift": query_drift,
    "narrative_today": narrative_today,
    "whats_interesting": whats_interesting,
}


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
