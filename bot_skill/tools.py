"""Hermes skill tools for skin-market.

Each public function decorated with ``@tool`` is a callable the Discord
bot's LLM can invoke. The bot reads ``SKILL.md`` to decide which tool
to call and what arguments to pass; the function returns a structured
value (dict / list / ``Attachment``) that the LLM renders for the user
according to the rules in ``SKILL.md``.

Tools wrap HTTP calls against the local read API
(http://localhost:8001 by default) with a static bearer token from the
``SKIN_MARKET_API_TOKEN`` env var. The token must match one of the
api container's accepted tokens (Phase 6.6 / Phase 7b, ADR 014 §10).

## Plausible Hermes shape — refactor if loader needs differ

We don't have a working Hermes skill to pattern-match against. The
choices below are the most-general guess:

- Plain Python functions with PEP 484 type hints and clear docstrings.
- A no-op ``@tool`` decorator that appends the function to
  ``TOOLS`` (module-level list) for any loader that wants a registry.
- Plain return values: ``dict``, ``list``, ``str``, or ``Attachment``
  for binary content.
- Typed exceptions: ``SkinMarketBotError`` and subclasses, so the
  bot's reply layer can match-and-format rather than parsing error
  strings.

If Hermes' loader rejects this shape, the function bodies don't need
to change — only the registration glue. ADR 015 §"Hermes integration"
documents the shape we landed on.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


DEFAULT_API_BASE_URL = "http://localhost:8001"

# v1: the three sources the project ships with. Three-state render
# walks this list to find sources NOT seen in /price + insights so
# they surface as "never observed yet" instead of being silently
# omitted. When a fourth source lands (CSFloat per ADR 010), update
# this list AND the denomination map below; otherwise it's a config
# change in the api / collector and a re-deploy.
EXPECTED_SOURCES: tuple[str, ...] = ("skinport", "dmarket", "steam_market")

# Source → denomination map for the never-observed case where we don't
# have a /price row to read it from. Mirrors ``sources.denomination``
# in the DB; if a source's denomination changes there, update here.
_DENOMINATION_BY_SOURCE: dict[str, str] = {
    "steam_market": "wallet_credit",
    "skinport": "usd",
    "dmarket": "usd",
}

# Freshness threshold — matches ``COMPARABLE_FRESHNESS_HOURS`` in
# ``api/routes/deals.py``. Observations older than this are tagged
# ``state="stale"`` so the bot can render a 🟡 marker per SKILL.md.
STALE_HOURS: int = 4

# Anomaly flag freshness — a divergence insight older than this is
# considered "ancient" and not surfaced. Z-scores reset between
# analytics cycles (hourly), so anything older than ~2h is stale.
ANOMALY_FRESHNESS_HOURS: int = 2


@dataclass(frozen=True)
class Attachment:
    """Binary tool output — e.g. ``render_chart`` returns a PNG.

    Hermes' reply path renders this as a Discord attachment. If the
    loader expects raw ``bytes`` instead, unwrap ``content``.
    """

    content: bytes
    media_type: str
    filename: str


TOOLS: list = []


def tool(fn):
    """Mark a function as a Hermes skill tool — appends to ``TOOLS``.

    Currently a marker only. If Hermes' loader needs per-tool metadata
    (description, JSON schema, parameter docs), attach them here once
    the loader's needs are known. The function body and signature
    don't depend on this decorator.
    """
    TOOLS.append(fn)
    return fn


# ---------------------------------------------------------------------
# Typed exceptions — the bot's reply layer catches these to format
# error responses per the rules in SKILL.md §"Error states".
# ---------------------------------------------------------------------


class SkinMarketBotError(Exception):
    """Base class. ``str(exc)`` is a human-readable message the bot
    can render verbatim if no specific subclass handler matches."""


class ApiUnreachableError(SkinMarketBotError):
    """Network-level failure: the api at ``base_url`` didn't respond.
    Bot suggests: try again in a moment, ping operator if persists."""


class ApiAuthError(SkinMarketBotError):
    """401 from the api — token mismatch or auth misconfigured. Bot
    suggests: operator must check ``SKIN_MARKET_API_TOKEN``."""


class ItemNotInWatchlistError(SkinMarketBotError):
    """404 on /items/{slug}/... — item not tracked. Bot suggests:
    request operator to add it (no in-bot watchlist-edit per v1
    scope; CLI is ``scripts/watchlist_edit.py``)."""


class ApiUnexpectedError(SkinMarketBotError):
    """5xx or other unexpected HTTP status. Bot suggests: try again,
    ping operator with the wrapped detail if persists."""


# ---------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------


def _client(timeout_read: float = 30.0) -> httpx.Client:
    """Authenticated httpx client. Token must be in
    ``SKIN_MARKET_API_TOKEN``; absent ⇒ raises ``ApiAuthError`` so
    the bot tells the operator to set the env var rather than failing
    obscurely on the next API call."""
    token = (os.environ.get("SKIN_MARKET_API_TOKEN") or "").strip()
    if not token:
        raise ApiAuthError(
            "SKIN_MARKET_API_TOKEN environment variable is not set. "
            "Set it to a valid token from the api container's "
            "configured set. See bot_skill/README.md."
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
    """GET ``path`` and return parsed JSON. Translates httpx
    network/HTTP errors into the bot's typed exceptions."""
    try:
        resp = client.get(path, params=params)
    except httpx.RequestError as exc:
        raise ApiUnreachableError(
            f"Couldn't reach the skin-market API at "
            f"{client.base_url!s}: {exc}"
        ) from exc

    if resp.status_code == 401:
        raise ApiAuthError(
            "API rejected the bearer token (401). Operator should "
            "verify SKIN_MARKET_API_TOKEN matches the api "
            "container's configured set."
        )
    if resp.status_code == 404:
        raise ItemNotInWatchlistError(
            f"Not found on the api: {path}. The item may not be on "
            f"the watchlist yet — operator path is "
            f"`scripts/watchlist_edit.py add ...`."
        )
    if resp.status_code >= 400:
        raise ApiUnexpectedError(
            f"Unexpected {resp.status_code} from {path}: "
            f"{resp.text[:200]}"
        )
    return resp.json()


# ---------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------


@tool
def list_watchlist() -> list[dict]:
    """Return every item currently on the watchlist.

    Each entry: ``{"slug", "market_hash_name", "display_name"}``. No
    pagination — watchlist is small enough that the bot can cache
    locally per session.
    """
    with _client() as c:
        return _get_json(c, "/items")


@tool
def query_current_price(slug: str) -> dict:
    """Three-state availability + per-source price snapshot for one item.

    Composes ``/items/{slug}/price`` (fresh observations) and
    ``/items/{slug}/insights`` (streak + divergence rows) into a
    single result the bot renders per SKILL.md §"Three-state
    availability render".

    Returned shape::

        {
            "slug": "ak-47-redline-field-tested",
            "display_name": "AK-47 | Redline (Field-Tested)",
            "per_source": [
                {
                    "source": "skinport",
                    "denomination": "usd",
                    "state": "fresh" | "stale" | "unavailable" |
                             "never_observed",
                    # state in {fresh, stale}:
                    "price": "33.06",
                    "volume": 521,
                    "observed_at": "2026-05-12T22:55:06Z",
                    "minutes_since_observed": 65,
                    # state in {unavailable}:
                    "streak_cycles": 3,
                    "last_seen_observed": "2026-05-12T10:00:00Z",
                    "first_seen_unavailable": "2026-05-12T13:00:00Z",
                    # state in {never_observed}:
                    # (no extra fields)
                },
                ...
            ],
            "anomaly_flag": None | {
                "z_score": "-2.89",
                "source_a_id": "1",
                "source_b_id": "27",
                "summary": "Cross-source spread is 2.9 stddev below baseline",
            },
        }
    """
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
        # Pick the largest |z| — the most striking divergence is the
        # one to flag if there are several active for the same item.
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


@tool
def query_price_history(
    slug: str,
    source: str | None = None,
    days: int = 7,
    limit: int = 500,
) -> dict:
    """Time-series observations for one item. Optional ``source``
    filter; defaults to all enabled sources. ``days`` becomes the
    ``since`` window (now − days); ``limit`` caps the response (max
    5000 enforced server-side)."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    params: dict[str, Any] = {"since": since, "limit": limit}
    if source is not None:
        params["source"] = source
    with _client() as c:
        return _get_json(c, f"/items/{slug}/history", params=params)


@tool
def render_chart(
    slug: str, source: str = "skinport", days: int = 7
) -> Attachment:
    """PNG chart of one (item, source) over the last N days. Returns
    an ``Attachment`` the bot uploads as a Discord image."""
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


@tool
def evaluate_deal(
    slug: str, amount: str, currency: str
) -> dict:
    """Run the deal evaluator. ``amount`` is a Decimal-as-string
    ("42.50"); ``currency`` is "usd" or "wallet_credit". The response
    carries ``verdict``, ``comparable``, ``informational``, and a
    pre-formatted ``summary`` string the bot can render directly."""
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


@tool
def narrative_today() -> dict:
    """Return the latest daily narrative (paragraph of English prose
    + citation meta). 404 from the API surfaces as
    ``ItemNotInWatchlistError`` — re-purposed for "no narrative yet";
    the bot's reply layer can match and render "no summary yet"."""
    with _client() as c:
        return _get_json(c, "/insights/narrative/latest")


@tool
def whats_interesting(hours: int = 6) -> dict:
    """Currently-firing cross-source divergences + volume anomalies
    from the last ``hours`` (default 6, max 24). Joined with item
    slug + display_name so the bot can render rows without per-item
    lookups."""
    with _client() as c:
        return _get_json(
            c, "/insights/anomalies/recent", params={"hours": hours}
        )


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """Parse the ISO 8601 timestamps the API emits, normalizing the
    trailing ``Z`` (FastAPI's default) to ``+00:00`` for
    ``datetime.fromisoformat`` portability across Python 3.10/3.11."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
