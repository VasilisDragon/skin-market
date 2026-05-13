"""Skin-market API tool wrappers — Phase 7c.

Seven Python functions that wrap HTTP calls against the local read
API (``http://api:8000`` from inside compose, ``http://localhost:8001``
from the host). The Discord bot's LLM router decides which to call;
``bot.ollama_client`` executes the call and feeds the result back.

The function bodies + typed exception hierarchy + three-state composer
for ``query_current_price`` are carried forward from the Phase 7b
Hermes attempt (now archived at
``docs/archive/bot_skill_hermes_attempt/``). What changed in 7c:

- The ``@tool`` decorator and ``TOOLS`` list are gone. Tools are
  declared as JSON-schema dicts in ``TOOL_DEFINITIONS`` (Ollama's
  request format) and a parallel ``TOOL_FUNCTIONS`` dict maps
  ``name → callable`` for the executor to dispatch by name.
- Tool function bodies stay synchronous; the bot wraps each call in
  ``asyncio.to_thread`` so a slow API call doesn't block the
  discord.py event loop.

ADR 016 documents the broader runtime design and the open-source
tool-calling defensive posture.
"""

from __future__ import annotations

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
# and feeds str(exc) back to Ollama as the tool_result so the model
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
# Tool function bodies
# ---------------------------------------------------------------------


def list_watchlist() -> list[dict]:
    with _client() as c:
        return _get_json(c, "/items")


def query_current_price(slug: str) -> dict:
    with _client() as c:
        price_data = _get_json(c, f"/items/{slug}/price")
        insights_data = _get_json(c, f"/items/{slug}/insights")

    fresh_by_source: dict[str, dict] = {
        s["source"]: s for s in price_data["sources"]
    }
    streak_by_source: dict[str, dict] = {}
    divergence_rows: list[dict] = []

    now = datetime.now(UTC)
    for insight in insights_data["insights"]:
        meta = insight.get("meta") or {}
        if insight["insight_type"] == "item_unavailability_streak":
            source_name = meta.get("source_name")
            if source_name:
                streak_by_source[source_name] = {
                    "streak_cycles": meta.get(
                        "streak_cycles", int(insight.get("value", 0))
                    ),
                    "last_seen_observed": meta.get("last_seen_observed"),
                    "first_seen_unavailable": meta.get(
                        "first_seen_unavailable"
                    ),
                }
        elif insight["insight_type"] == "cross_source_divergence":
            computed_at = _parse_iso(insight["computed_at"])
            age_h = (now - computed_at).total_seconds() / 3600
            if age_h <= ANOMALY_FRESHNESS_HOURS:
                divergence_rows.append(insight)

    per_source: list[dict] = []
    for source_name in EXPECTED_SOURCES:
        if source_name in fresh_by_source:
            row = fresh_by_source[source_name]
            observed_at = _parse_iso(row["observed_at"])
            minutes = int((now - observed_at).total_seconds() / 60)
            per_source.append(
                {
                    "source": source_name,
                    "denomination": row["denomination"],
                    "state": (
                        "stale"
                        if minutes > STALE_HOURS * 60
                        else "fresh"
                    ),
                    "price": row["price"],
                    "volume": row["volume"],
                    "observed_at": row["observed_at"],
                    "minutes_since_observed": minutes,
                }
            )
        elif source_name in streak_by_source:
            s = streak_by_source[source_name]
            per_source.append(
                {
                    "source": source_name,
                    "denomination": _DENOMINATION_BY_SOURCE.get(
                        source_name
                    ),
                    "state": "unavailable",
                    "streak_cycles": s["streak_cycles"],
                    "last_seen_observed": s["last_seen_observed"],
                    "first_seen_unavailable": s[
                        "first_seen_unavailable"
                    ],
                }
            )
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

    return {
        "slug": price_data["slug"],
        "display_name": price_data["display_name"],
        "per_source": per_source,
        "anomaly_flag": anomaly_flag,
    }


def query_price_history(
    slug: str,
    source: str | None = None,
    days: int = 7,
    limit: int = 500,
) -> dict:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    params: dict[str, Any] = {"since": since, "limit": limit}
    if source is not None:
        params["source"] = source
    with _client() as c:
        return _get_json(c, f"/items/{slug}/history", params=params)


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
        return resp.json()


def narrative_today() -> dict:
    with _client() as c:
        return _get_json(c, "/insights/narrative/latest")


def whats_interesting(hours: int = 6) -> dict:
    with _client() as c:
        return _get_json(
            c, "/insights/anomalies/recent", params={"hours": hours}
        )


# ---------------------------------------------------------------------
# Ollama tool declarations + dispatch table
# ---------------------------------------------------------------------


# Ollama's chat API expects tools in OpenAI-compatible JSON-schema
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
                "Return every item the system tracks. Each entry has "
                "{slug, market_hash_name, display_name}. Call this "
                "when the user asks 'what do you track?' / 'list "
                "items' / 'what items are available?'. Also useful "
                "when you need to find the exact slug for an item "
                "the user named informally."
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
                "Get the current per-source price snapshot for one "
                "item. Returns prices from each source (Skinport, "
                "DMarket in USD; Steam in wallet credit), each with "
                "freshness, plus an anomaly flag when a divergence "
                "is currently active. Call this when the user asks "
                "about a specific item's price — 'how much is X?', "
                "'what's the price of X?', 'X price'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Item slug. Lowercase, hyphens for "
                            "spaces/punctuation, special characters "
                            "stripped. E.g. 'ak-47-redline-field-"
                            "tested', 'star-karambit-doppler-"
                            "factory-new', 'stattrak-ak-47-redline-"
                            "field-tested'. If unsure, call "
                            "list_watchlist first."
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
                "Time-series of prices for one item over a window. "
                "Call this when the user asks about price movement, "
                "history, trends — 'how has X moved?', 'X history', "
                "'X trend this week'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": (
                            "Optional source filter: 'skinport', "
                            "'dmarket', or 'steam_market'. Omit for "
                            "all sources."
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
                "Generate a PNG price chart for ONE source over N "
                "days. Single-source by design — denominations "
                "differ across sources. Call when the user asks for "
                "a chart, plot, or graph. The PNG is attached to "
                "your reply automatically; you should still add a "
                "short text comment describing what the chart shows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": (
                            "Source to plot — 'skinport' (default), "
                            "'dmarket', or 'steam_market'."
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
                "Run the opinionated deal evaluator. Returns "
                "verdict ('below_market'|'at_market'|'above_market'|"
                "'no_comparable_data') plus a pre-formatted summary "
                "string. Call when the user asks whether a price is "
                "fair — 'is $30 a good price for X?', 'should I pay "
                "45 SC for X?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "amount": {
                        "type": "string",
                        "description": (
                            "Decimal as string. '42.50', '500'. "
                            "Don't pass a float — precision matters."
                        ),
                    },
                    "currency": {
                        "type": "string",
                        "enum": ["usd", "wallet_credit"],
                        "description": (
                            "'usd' for $-amounts on Skinport/DMarket "
                            "or USD-context questions. "
                            "'wallet_credit' for Steam wallet SC."
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
                "Return the latest daily English-prose market "
                "summary (generated nightly at 02:00 UTC). Call when "
                "the user asks 'what happened today?', 'daily "
                "summary', 'market recap', 'anything new'."
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
            "name": "whats_interesting",
            "description": (
                "Return currently-firing market anomalies — "
                "cross-source divergences (one source diverging "
                "from baseline against another) and volume "
                "anomalies (Steam 24h sales outside the rolling "
                "baseline). Each row includes the item, the z-score, "
                "and the source pair. Call when the user asks "
                "'anything interesting?', 'what's moving?', 'any "
                "anomalies?', 'what's weird today?'."
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
    "narrative_today": narrative_today,
    "whats_interesting": whats_interesting,
}


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
