"""Pricempire bulk-snapshot collector — Phase 2a.

Pricempire serves a single-shot bulk endpoint
(``GET /v4/paid/items/prices``) that returns the entire CS2 catalog —
~39,400 items, ~33 MB — in one response. There is no pagination, no
per-item endpoint, no app_id filter. ADR 018 has the architectural
context; ADR 019 has this module's design.

This collector deliberately does NOT extend ``collectors.base.Collector``.
The BaseCollector abstraction is per-item-per-source (one HTTP call →
one PriceObservation → one prices row), and Pricempire's shape (one
HTTP call → ~39k items × up to 6 sub-providers → many rows) doesn't
fit. Forcing the abstraction would either bloat BaseCollector with a
"bulk mode" branch or pretend each item is a separate HTTP call —
neither helps. The scheduler's job-per-source pattern is preserved by
treating ``pricempire`` as a pseudo-source in the ``sources`` table
(see migration 0005 and ADR 018 §3).

Why ijson:

The response is ~33 MB. Loading it as a Python ``dict`` would peak
around 100-150 MB of resident memory for transient parse state. ijson
streams the top-level array, yielding one item dict at a time, so the
collector's footprint stays in the low-MB range throughout. The
``yajl2_c`` backend is used when available (it ships with most ijson
installs); ijson falls back to pure-Python parsing otherwise. The
default backend is fine.

Dedup gate:

Same shape as the prices collectors (ADR 009 §3): skip insert when
``(price, count)`` matches the latest existing row for that
``(item_id, source_id)`` pair. Phase 1's dedup-vs-display lessons
(ADR 017) apply — but there is no observation_log analog for
Pricempire in Phase 2a. Phase 2b decides whether one is needed based
on what drift detection actually requires. Until then the dedup gate
compares against ``pricempire_observations`` itself.

Failure handling:

- 4xx/5xx → log WARNING, exit cleanly. No retry inside a single
  cycle; the next 15-minute cycle is the retry.
- httpx.RequestError → same. Logged at WARNING with the error type.
- Missing PRICEMPIRE_API_KEY → fail fast at module-load time so a
  misconfigured deploy doesn't silently run zero-effective cycles.
- Unknown item (market_hash_name not in our ``items`` table) → skip.
  Phase 2a only ingests Pricempire data for items we already curate;
  Phase 2b adds the indexed-item layer for the long tail.
- Unknown provider_key (not one of the six known sub-providers) →
  skip with a counter. Logged once per cycle; Pricempire may add
  providers without warning, and we don't want to write rows we
  can't account for.

Cycle-complete log line:

Mirrors the existing collectors' ``"X cycle complete: ..."`` shape so
the scheduler's log format stays uniform.
"""

from __future__ import annotations

import io
import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import ijson
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.models import Item, PricempireObservation, Source
from db.naming import normalize_name

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

PRICEMPIRE_BASE_URL = "https://api.pricempire.com"
PRICEMPIRE_PRICES_PATH = "/v4/paid/items/prices"

# CS2 app id. The Pricempire diagnostic confirmed `?app_id=730` is a
# no-op on this endpoint (the catalog is already CS2-only), but we
# pass it anyway as a defensive hint — if Pricempire ever activates
# the filter we automatically remain CS2-scoped.
_APP_ID = "730"

# The six sub-providers we ingest. The wire key (e.g. "buff163") maps
# to the corresponding source name in our sources table
# (e.g. "pricempire_buff163"). Provider keys come from the diagnostic
# samples — the ones with consistent CS2 coverage. New providers
# Pricempire adds in the future will be logged but skipped until added
# here AND to the sources table (migration update).
_PROVIDER_KEY_TO_SOURCE_NAME: dict[str, str] = {
    "buff163": "pricempire_buff163",
    "buff163_buy": "pricempire_buff163_buy",
    "skinport": "pricempire_skinport",
    "dmarket": "pricempire_dmarket",
    "csmoney": "pricempire_csmoney",
    # Pricempire's wire key is "swapgg" (no dot, no underscore).
    # Verified empirically — `sources=swap.gg` returns HTTP 400 with
    # the accepted-values list; `sources=swapgg` returns 200. Our
    # source name "pricempire_swap_gg" stays in underscore form for
    # Postgres consistency. This dict is the single point of
    # translation between Pricempire's wire vocabulary and our
    # internal source names.
    "swapgg": "pricempire_swap_gg",
}

# Comma-joined wire keys for the request's `sources=` query param.
# The order doesn't matter to Pricempire; alphabetical for stability.
_SOURCES_PARAM = ",".join(sorted(_PROVIDER_KEY_TO_SOURCE_NAME.keys()))

# Pricempire returns prices in cents (empirically confirmed in the
# diagnostic samples — e.g. 17316 for $173.16). Divide before insert.
_CENTS_PER_DOLLAR = Decimal("100")

# Read timeout for the bulk call. The diagnostic averaged 3-9s on the
# prices endpoint; 60s is comfortable headroom for cold-cache or
# slow-network days without masking a genuinely-stuck request.
_HTTP_TIMEOUT_SECONDS = 60.0


# ────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────


def collect_snapshot() -> None:
    """Run one Pricempire bulk-snapshot cycle.

    Synchronous, blocking, idempotent under the dedup gate. Designed
    to be called once per APScheduler tick.
    """
    api_key = os.environ.get("PRICEMPIRE_API_KEY", "").strip()
    if not api_key:
        # Fail fast — silent zero-row cycles would mask a deploy
        # misconfiguration for hours before anyone noticed.
        logger.error(
            "Pricempire cycle aborted: PRICEMPIRE_API_KEY is unset. "
            "Set it in .env and restart the collector container."
        )
        return

    logger.info("Pricempire cycle starting")
    cycle_started = time.monotonic()

    engine = get_engine()
    with Session(engine) as session:
        item_id_by_name = _load_item_index(session)
        source_id_by_name = _load_pricempire_source_index(session)

    if not item_id_by_name:
        logger.warning(
            "Pricempire cycle: items table is empty, nothing to ingest"
        )
        return
    if not source_id_by_name:
        logger.error(
            "Pricempire cycle: pricempire_* source rows missing from "
            "sources table. Re-run migration 0005."
        )
        return

    # Counters for the cycle-complete log line.
    items_seen = 0
    items_skipped_unknown = 0
    rows_written = 0
    rows_unchanged = 0
    rows_unknown_provider = 0
    unknown_providers_seen: set[str] = set()

    try:
        with _make_client(api_key) as client:
            stream = _stream_prices(client)

            with Session(engine) as session:
                for item in stream:
                    items_seen += 1
                    market_hash_name = item.get("market_hash_name")
                    if not market_hash_name:
                        continue

                    canonical_name = normalize_name(market_hash_name)
                    item_id = item_id_by_name.get(canonical_name)
                    if item_id is None:
                        # Phase 2a only ingests curated-watchlist
                        # items. Phase 2b adds the long-tail layer.
                        items_skipped_unknown += 1
                        continue

                    prices = item.get("prices") or []
                    for row in prices:
                        outcome = _persist_row(
                            session=session,
                            item_id=item_id,
                            source_id_by_name=source_id_by_name,
                            wire_row=row,
                            unknown_providers_seen=unknown_providers_seen,
                        )
                        if outcome == "written":
                            rows_written += 1
                        elif outcome == "unchanged":
                            rows_unchanged += 1
                        else:  # unknown_provider
                            rows_unknown_provider += 1

                    # Commit per item so a mid-cycle SIGKILL keeps
                    # partial progress, matching the prices
                    # collectors' commit cadence.
                    session.commit()
    except httpx.RequestError as exc:
        logger.warning(
            "Pricempire cycle: HTTP transport error %s — exiting "
            "cleanly, next cycle retries",
            type(exc).__name__,
        )
        return
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Pricempire cycle: %d from %s — exiting cleanly, next "
            "cycle retries",
            exc.response.status_code,
            exc.request.url,
        )
        return
    except Exception:
        # Defensive: anything else (parser error, DB error) gets
        # logged with traceback so an operator can diagnose without
        # the scheduler losing track.
        logger.exception(
            "Pricempire cycle: unexpected error mid-stream after "
            "%d items, %d rows written",
            items_seen,
            rows_written,
        )
        return

    cycle_seconds = time.monotonic() - cycle_started
    if unknown_providers_seen:
        logger.warning(
            "Pricempire cycle: encountered unknown provider_key "
            "value(s) — %s. Add to _PROVIDER_KEY_TO_SOURCE_NAME and "
            "sources table to ingest.",
            sorted(unknown_providers_seen),
        )
    logger.info(
        "Pricempire cycle complete: %d items seen, %d skipped "
        "(not in watchlist), %d rows written, %d unchanged, "
        "%d skipped (unknown provider), elapsed %.1fs",
        items_seen,
        items_skipped_unknown,
        rows_written,
        rows_unchanged,
        rows_unknown_provider,
        cycle_seconds,
    )


# ────────────────────────────────────────────────────────────────────
# HTTP plumbing
# ────────────────────────────────────────────────────────────────────


def _make_client(api_key: str) -> httpx.Client:
    """One client per cycle. Bearer auth, JSON accept, generous read
    timeout for the 33 MB payload."""
    return httpx.Client(
        base_url=PRICEMPIRE_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            # Pricempire serves gzip; httpx negotiates this
            # automatically when the body is large enough.
        },
        timeout=httpx.Timeout(
            connect=10.0,
            read=_HTTP_TIMEOUT_SECONDS,
            write=10.0,
            pool=10.0,
        ),
    )


def _stream_prices(client: httpx.Client) -> Iterator[dict[str, Any]]:
    """Yield one decoded item dict at a time from the bulk endpoint.

    Pricempire's response is ~33 MB raw bytes (well within memory
    budget on the deploy host), but parsing it into a Python dict
    peaks around 100-150 MB of resident objects. ijson streams the
    JSON array element-by-element so the **Python object** peak stays
    in the low MB range — that's the actual memory win.

    Backend choice (raw bytes vs iter_bytes vs file-like): the ijson
    yajl backend's iterable-bytes path is fragile across versions, so
    we read the full body into a BytesIO and let ijson stream over
    that. We keep the win on the parsed-object side; we give up the
    win on raw bytes (33 MB is fine).

    Raises ``httpx.HTTPStatusError`` if Pricempire returns non-2xx,
    which the caller handles cleanly.
    """
    response = client.get(
        PRICEMPIRE_PRICES_PATH,
        params={"app_id": _APP_ID, "sources": _SOURCES_PARAM},
    )
    response.raise_for_status()
    # ``use_float=True`` returns native Python floats for JSON numbers
    # with a fractional part (e.g. ``liquidity``, ``meta.rate``). Default
    # behavior produces ``Decimal``, which isn't JSON-serializable when
    # we round-trip the wire row into the ``raw_response`` JSONB column.
    # Native float is fine for the raw_response use case (we store, we
    # don't arithmetic on these values). Our own ``price`` field is
    # parsed separately into Decimal via Decimal(str(...)) to preserve
    # precision regardless of int/float on the wire.
    yield from ijson.items(
        io.BytesIO(response.content), "item", use_float=True
    )


# ────────────────────────────────────────────────────────────────────
# DB write path
# ────────────────────────────────────────────────────────────────────


def _persist_row(
    *,
    session: Session,
    item_id: Any,
    source_id_by_name: dict[str, int],
    wire_row: dict[str, Any],
    unknown_providers_seen: set[str],
) -> str:
    """Translate one Pricempire ``prices[]`` row into a DB write.

    Returns one of ``"written"``, ``"unchanged"``, ``"unknown_provider"``.
    """
    provider_key = wire_row.get("provider_key")
    if not provider_key:
        unknown_providers_seen.add("(missing)")
        return "unknown_provider"

    source_name = _PROVIDER_KEY_TO_SOURCE_NAME.get(provider_key)
    if source_name is None:
        unknown_providers_seen.add(provider_key)
        return "unknown_provider"

    source_id = source_id_by_name.get(source_name)
    if source_id is None:
        # The mapping table says this provider should land in
        # sources, but it's not there. Migration drift; log once.
        unknown_providers_seen.add(source_name)
        return "unknown_provider"

    raw_price = wire_row.get("price")
    if raw_price is None:
        return "unchanged"  # Pricempire knows the provider but has no price; nothing to record.

    # Wire prices are integer cents in practice (empirically confirmed
    # in the diagnostic). Defensively, parse through ``Decimal(str(...))``
    # so we survive a future Pricempire change that switches to dollar
    # floats — Decimal(str(173.16)) preserves precision, whereas
    # int(173.16) would truncate the cents.
    try:
        price_cents = Decimal(str(raw_price))
        price = (price_cents / _CENTS_PER_DOLLAR).quantize(
            Decimal("0.01")
        )
    except Exception:
        # Malformed numeric — bail. Logging per row is too noisy at
        # 40k items × 6 providers; the row just doesn't get persisted.
        return "unchanged"

    count = wire_row.get("count")
    count_int: int | None
    if isinstance(count, int):
        count_int = count
    elif isinstance(count, str) and count.isdigit():
        count_int = int(count)
    else:
        count_int = None

    # Dedup gate. Same shape as collectors.base.should_write_observation
    # but local to this collector — no observation_log analog yet
    # (Phase 2b decision). We compare against the latest row for
    # (item_id, source_id).
    latest = session.execute(
        select(
            PricempireObservation.price,
            PricempireObservation.count,
        )
        .where(
            PricempireObservation.item_id == item_id,
            PricempireObservation.source_id == source_id,
        )
        .order_by(PricempireObservation.timestamp.desc())
        .limit(1)
    ).first()
    if (
        latest is not None
        and latest.price == price
        and latest.count == count_int
    ):
        return "unchanged"

    insert_stmt = (
        pg_insert(PricempireObservation)
        .values(
            item_id=item_id,
            source_id=source_id,
            timestamp=datetime.now(UTC),
            price=price,
            count=count_int,
            updated_at=_parse_iso(wire_row.get("updated_at")),
            last_checked_at=_parse_iso(wire_row.get("last_checked_at")),
            currency="USD",
            raw_response=wire_row,
        )
        # Race-safety: if two cycles ever collide on the same
        # microsecond timestamp (extraordinarily unlikely), silently
        # no-op rather than crash.
        .on_conflict_do_nothing(
            index_elements=["item_id", "source_id", "timestamp"]
        )
    )
    session.execute(insert_stmt)
    return "written"


# ────────────────────────────────────────────────────────────────────
# DB read helpers (one-shot at cycle start)
# ────────────────────────────────────────────────────────────────────


def _load_item_index(session: Session) -> dict[str, Any]:
    """Map normalized market_hash_name → items.id for every watchlist
    item. One query per cycle, ~48 rows today.

    Normalization mirrors collectors/base.py's lookup pattern (NFC via
    db.naming.normalize_name). Pricempire's market_hash_name values
    are NFC-normalized in practice but we apply the same normalizer
    defensively.
    """
    rows = session.execute(
        select(Item.id, Item.market_hash_name)
    ).all()
    return {normalize_name(row.market_hash_name): row.id for row in rows}


def _load_pricempire_source_index(session: Session) -> dict[str, int]:
    """Map source name ('pricempire_buff163' etc.) → sources.id. One
    query per cycle. The sub-provider names live in
    ``_PROVIDER_KEY_TO_SOURCE_NAME.values()`` so adding a new provider
    means editing that dict; the pseudo-source row 'pricempire' is
    intentionally excluded — it carries no observations, only the
    schedule."""
    rows = session.execute(
        select(Source.id, Source.name).where(
            Source.name.in_(_PROVIDER_KEY_TO_SOURCE_NAME.values())
        )
    ).all()
    return {row.name: row.id for row in rows}


# ────────────────────────────────────────────────────────────────────
# Misc
# ────────────────────────────────────────────────────────────────────


def _parse_iso(s: Any) -> datetime | None:
    """Parse an ISO 8601 string into a tz-aware datetime, or None for
    missing/malformed values. Pricempire's ``updated_at`` and
    ``last_checked_at`` are always 'Z'-suffixed; replace with
    +00:00 for fromisoformat."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
