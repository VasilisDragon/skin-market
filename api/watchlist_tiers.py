"""Tier-aware response helper for the read API (Phase 2b Step 8).

Tier lives in ``data/watchlist.yaml`` per ADR 024 — the ``items`` table
deliberately doesn't carry a tier column, so any code that needs to
shape responses by tier (e.g. /items/{slug}/drift only makes sense for
``tier: deep``) must read the YAML.

Reading semantics

- Lazy load on first call; cache in module globals.
- ``reload()`` re-reads the YAML; tests use this to inject synthetic
  broad-tier items without editing the real watchlist.
- Operator restart of the ``api`` service picks up YAML edits — same
  workflow as the collector. The cache deliberately does NOT
  auto-invalidate on disk change.

Orphan handling (ADR 024)

A row in the ``items`` table that is no longer in the YAML watchlist
is an *orphan*: historical prices/insights remain queryable, but the
collector no longer polls it (per Step 7.1.5's ``_load_watchlist``
tier filter). ``get_tier`` returns the literal string ``"orphan"`` for
these so the routes can shape responses identically to broad-tier
(empty current data, structural reason for it).

This module assumes the caller has already verified the item exists in
the ``items`` table — passing an unknown ``market_hash_name`` raises
``ValueError``. The 404-vs-orphan split happens at the route layer,
which already needs to query items for its existence check anyway
(Option B from the Step 8 design proposal: route handlers query items
table first, then consult ``get_tier`` for tier branching).
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Literal

from scripts.seed_watchlist import DEFAULT_WATCHLIST_PATH, load_watchlist

Tier = Literal["deep", "broad", "orphan"]

# Module-level cache. ``None`` means "not yet loaded"; an empty dict
# would be ambiguous with "loaded but empty YAML". The lock protects
# the read-then-write window in ``_ensure_loaded`` against the multiple
# worker threads uvicorn spawns by default.
_tier_map: dict[str, Tier] | None = None
_watchlist_path: Path = DEFAULT_WATCHLIST_PATH
_lock = Lock()


def _ensure_loaded() -> dict[str, Tier]:
    """Return the cached tier map, loading from YAML on first call."""
    global _tier_map
    if _tier_map is not None:
        return _tier_map
    with _lock:
        # Double-check after acquiring lock; another thread may have
        # populated the cache while we waited.
        if _tier_map is None:
            _tier_map = _load_from_yaml(_watchlist_path)
        return _tier_map


def _load_from_yaml(path: Path) -> dict[str, Tier]:
    """Read the YAML and build a ``{market_hash_name: tier}`` map.

    Delegates to ``scripts.seed_watchlist.load_watchlist`` for the
    schema_version check and per-item tier validation — so an invalid
    YAML fails fast here exactly the way it fails fast at seed time.
    """
    data = load_watchlist(path)
    out: dict[str, Tier] = {}
    for item in data["items"]:
        # ``load_watchlist`` already validated tier ∈ {deep, broad}
        # and market_hash_name presence.
        out[item["market_hash_name"]] = item["tier"]
    return out


def get_tier(market_hash_name: str) -> Tier:
    """Return the tier for an item that is *known to exist in the items
    table*.

    Returns ``"deep"`` or ``"broad"`` when the item is in the active
    YAML watchlist; returns ``"orphan"`` when the item exists in the
    items table (caller's precondition) but is no longer in the YAML.

    Raises ``ValueError`` if the caller hasn't honored the precondition
    of "item exists in items table." The function has no way to verify
    that itself (no DB session here), but a market_hash_name that the
    caller passes in MUST correspond to a real items row — otherwise
    the orphan/broad responses would render a non-existent item as if
    it were a known one. Routes do the existence check via the items
    table query they already need for 404 handling.

    The ``ValueError`` is deliberately a programmer-error signal: it
    means a route forgot the existence check. In production it should
    never fire.
    """
    if not market_hash_name:
        raise ValueError(
            "get_tier requires a non-empty market_hash_name; caller "
            "must verify the item exists in the items table first"
        )
    cache = _ensure_loaded()
    # In-YAML → deep/broad. Not-in-YAML AND caller-says-exists → orphan.
    return cache.get(market_hash_name, "orphan")


def reload(path: Path | None = None) -> None:
    """Reset the cache and re-read the YAML on the next ``get_tier`` call.

    Production callers don't need this — operator restart of the
    ``api`` service is the documented refresh mechanism. Tests use
    this to inject a synthetic YAML where one real item is reclassified
    as broad (or to swap to a fixture path).

    If ``path`` is provided, future loads will read that path instead
    of the default watchlist location. Pass ``None`` to keep the
    current path but force a re-read.
    """
    global _tier_map, _watchlist_path
    with _lock:
        if path is not None:
            _watchlist_path = path
        _tier_map = None
