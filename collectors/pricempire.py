"""Pricempire bulk-snapshot collector.

Pricempire exposes a single bulk endpoint for the CS2 catalog. This
collector streams that payload, maps known sub-providers to internal
sources, and writes provider-specific observations.

The collector is intentionally separate from ``collectors.base``:
Pricempire is one request producing many item/source rows, while the
base collector contract is one item request producing one observation.
ADR 018 and ADR 019 document the architecture.
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
from db.models import (
    Item,
    PricempireItemMetadata,
    PricempireObservation,
    PricempireObservationLog,
    Source,
)
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

# Stable display order for the per-provider cycle summary.
_PROVIDER_ORDER: tuple[str, ...] = tuple(
    sorted(_PROVIDER_KEY_TO_SOURCE_NAME.values())
)

# Pricempire returns prices in cents (empirically confirmed in the
# diagnostic samples — e.g. 17316 for $173.16). Divide before insert.
_CENTS_PER_DOLLAR = Decimal("100")

# Read timeout for the bulk call. The diagnostic averaged 3-9s on the
# prices endpoint; 60s is comfortable headroom for cold-cache or
# slow-network days without masking a genuinely-stuck request.
_HTTP_TIMEOUT_SECONDS = 60.0


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
    # Per-provider breakdowns of rows_written / rows_unchanged. Keyed
    # by source name (e.g. "pricempire_buff163") in the order defined
    # by _PROVIDER_ORDER so the log line is stable cycle-to-cycle even
    # when a provider writes zero rows in a given cycle.
    written_by_provider: dict[str, int] = {
        name: 0 for name in _PROVIDER_ORDER
    }
    unchanged_by_provider: dict[str, int] = {
        name: 0 for name in _PROVIDER_ORDER
    }
    # Metadata-extraction counters (ADR 020). Metadata fields change
    # slowly so the dedup gate suppresses most writes in steady state.
    metadata_written = 0
    metadata_unchanged = 0

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
                        items_skipped_unknown += 1
                        continue

                    prices = item.get("prices") or []
                    for row in prices:
                        outcome, source_name = _persist_row(
                            session=session,
                            item_id=item_id,
                            source_id_by_name=source_id_by_name,
                            wire_row=row,
                            unknown_providers_seen=unknown_providers_seen,
                        )
                        if outcome == "written":
                            rows_written += 1
                            if source_name is not None:
                                written_by_provider[source_name] += 1
                        elif outcome == "unchanged":
                            rows_unchanged += 1
                            # "unchanged" without a source_name is the
                            # malformed-numeric / missing-price fallback
                            # (`_persist_row` returns ("unchanged", None)
                            # before resolving the provider). Per-provider
                            # bucket gets nothing in that case.
                            if source_name is not None:
                                unchanged_by_provider[source_name] += 1
                        else:  # unknown_provider
                            rows_unknown_provider += 1

                    # Metadata changes slowly, so this side write is
                    # cheap after the first populated cycle.
                    if _persist_metadata(
                        session=session,
                        item_id=item_id,
                        wire_item=item,
                    ):
                        metadata_written += 1
                    else:
                        metadata_unchanged += 1

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
        # Keep the scheduler process alive on parser or database errors.
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
    written_breakdown = _format_provider_breakdown(written_by_provider)
    unchanged_breakdown = _format_provider_breakdown(unchanged_by_provider)
    logger.info(
        "Pricempire cycle complete: %d items seen, %d skipped "
        "(not in watchlist), %d rows written (%s), %d unchanged "
        "(%s), %d skipped (unknown provider); metadata: %d written, "
        "%d unchanged; elapsed %.1fs",
        items_seen,
        items_skipped_unknown,
        rows_written,
        written_breakdown,
        rows_unchanged,
        unchanged_breakdown,
        rows_unknown_provider,
        metadata_written,
        metadata_unchanged,
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


def _upsert_observation_log(
    session: Session,
    item_id: Any,
    source_id: int,
    last_checked_at: datetime | None,
) -> bool:
    """Upsert (item_id, source_id) into pricempire_observation_log
    with Pricempire's claimed ``last_checked_at``.

    LOAD-BEARING INVARIANT: this helper MUST be called before the
    dedup SELECT in ``_persist_row``. Otherwise dedup-suppressed cycles
    leave the freshness log stale even when Pricempire is still
    actively polling. See ADR 023.

    Skips the upsert when ``last_checked_at`` is None (e.g. wire row
    omitted the field or it didn't parse as ISO 8601). The drift
    detector treats a missing log row as "stale" — same semantic as
    a row whose last_observed_at is too old to be fresh. Filing the
    "we polled but Pricempire didn't tell us when" case as stale is
    the honest behavior; pretending it's fresh would mask the
    underlying wire-format problem.

    Returns True on upsert, False on skip-due-to-None.
    """
    if last_checked_at is None:
        return False
    stmt = (
        pg_insert(PricempireObservationLog)
        .values(
            item_id=item_id,
            source_id=source_id,
            last_observed_at=last_checked_at,
        )
        .on_conflict_do_update(
            index_elements=["item_id", "source_id"],
            set_={"last_observed_at": last_checked_at},
        )
    )
    session.execute(stmt)
    return True


def _persist_row(
    *,
    session: Session,
    item_id: Any,
    source_id_by_name: dict[str, int],
    wire_row: dict[str, Any],
    unknown_providers_seen: set[str],
) -> tuple[str, str | None]:
    """Translate one Pricempire ``prices[]`` row into a DB write.

    Returns ``(outcome, source_name)`` where:
    - ``outcome`` is one of ``"written"``, ``"unchanged"``,
      ``"unknown_provider"``.
    - ``source_name`` is the resolved sub-provider source name (e.g.
      ``"pricempire_buff163"``) when the provider was recognized, else
      ``None``. The caller uses this for the per-provider counters in
      the cycle-complete log line.
    """
    provider_key = wire_row.get("provider_key")
    if not provider_key:
        unknown_providers_seen.add("(missing)")
        return "unknown_provider", None

    source_name = _PROVIDER_KEY_TO_SOURCE_NAME.get(provider_key)
    if source_name is None:
        unknown_providers_seen.add(provider_key)
        return "unknown_provider", None

    source_id = source_id_by_name.get(source_name)
    if source_id is None:
        # The mapping table says this provider should land in
        # sources, but it's not there. Migration drift; log once.
        unknown_providers_seen.add(source_name)
        return "unknown_provider", None

    # Keep freshness independent from price-row deduplication.
    _upsert_observation_log(
        session=session,
        item_id=item_id,
        source_id=source_id,
        last_checked_at=_parse_iso(wire_row.get("last_checked_at")),
    )

    raw_price = wire_row.get("price")
    if raw_price is None:
        # Pricempire knows the provider but has no price; nothing to
        # record. Treated as "unchanged" for the aggregate count but
        # source_name=None so the per-provider unchanged bucket isn't
        # inflated by no-price rows. (The provider IS resolved, but
        # nothing happened in our table either way.)
        return "unchanged", None

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
        return "unchanged", None

    count = wire_row.get("count")
    count_int: int | None
    if isinstance(count, int):
        count_int = count
    elif isinstance(count, str) and count.isdigit():
        count_int = int(count)
    else:
        count_int = None

    # Dedup gate. Same shape as collectors.base.should_write_observation
    # but local to this collector. The observation log is advanced
    # above (pre-dedup); this gate decides whether to write a new
    # pricempire_observations row based on (price, count) equality
    # with the latest existing row for (item_id, source_id).
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
        return "unchanged", source_name

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
    return "written", source_name


def _format_provider_breakdown(counts: dict[str, int]) -> str:
    """Render a stable ``"buff163=8, buff163_buy=6, ..."`` fragment from
    a {source_name: count} dict. The "pricempire_" prefix is stripped
    for readability — the cycle-complete line already names "Pricempire
    cycle complete" so the prefix would be redundant noise. Iteration
    order follows ``_PROVIDER_ORDER`` so the log line is stable across
    cycles even when individual providers write zero rows."""
    return ", ".join(
        f"{name.removeprefix('pricempire_')}={counts.get(name, 0)}"
        for name in _PROVIDER_ORDER
    )


# Item-level metadata fields lifted from each Pricempire wire item
# into typed columns on ``pricempire_item_metadata``. Order matters
# only for the dedup tuple (which compares the full column set in a
# stable order); the column names match the schema 1:1.
_METADATA_INT_FIELDS: tuple[str, ...] = (
    "rank",
    "marketcap",
    "count",
    "trades_7d",
    "trades_30d",
    "trades_90d",
    "steam_last_24h",  # always absent on /prices today (ADR 020)
    "steam_last_7d",
    "steam_last_30d",
    "steam_last_90d",
)


def _persist_metadata(
    *,
    session: Session,
    item_id: Any,
    wire_item: dict[str, Any],
) -> bool:
    """Extract slow-changing metadata from one wire item and persist a
    row to ``pricempire_item_metadata`` IF any field changed since the
    most recent row. Returns True if a row was written, False if the
    dedup gate caught a no-op.

    Pricempire's wire types are inconsistent across endpoints: most
    integer-valued fields arrive as numeric strings on /prices
    (``"rank": "554"``) but as native numbers on /metas
    (``"rank": 23219``). The parser handles int, float, numeric str,
    and None uniformly. Malformed values fall back to None rather
    than crashing the cycle.
    """
    extracted = {
        field: _coerce_int(wire_item.get(field))
        for field in _METADATA_INT_FIELDS
    }
    extracted["liquidity"] = _coerce_decimal(wire_item.get("liquidity"))

    # Dedup gate: tuple-compare against the most recent row for this
    # item. Order matters for the comparison; we use the column list
    # plus liquidity, all in the same canonical order both here and
    # in the latest-row SELECT below.
    latest = session.execute(
        select(
            PricempireItemMetadata.rank,
            PricempireItemMetadata.liquidity,
            PricempireItemMetadata.marketcap,
            PricempireItemMetadata.count,
            PricempireItemMetadata.trades_7d,
            PricempireItemMetadata.trades_30d,
            PricempireItemMetadata.trades_90d,
            PricempireItemMetadata.steam_last_24h,
            PricempireItemMetadata.steam_last_7d,
            PricempireItemMetadata.steam_last_30d,
            PricempireItemMetadata.steam_last_90d,
        )
        .where(PricempireItemMetadata.item_id == item_id)
        .order_by(PricempireItemMetadata.timestamp.desc())
        .limit(1)
    ).first()

    if latest is not None:
        new_tuple = (
            extracted["rank"],
            extracted["liquidity"],
            extracted["marketcap"],
            extracted["count"],
            extracted["trades_7d"],
            extracted["trades_30d"],
            extracted["trades_90d"],
            extracted["steam_last_24h"],
            extracted["steam_last_7d"],
            extracted["steam_last_30d"],
            extracted["steam_last_90d"],
        )
        if tuple(latest) == new_tuple:
            return False  # unchanged — dedup gate caught it

    insert_stmt = (
        pg_insert(PricempireItemMetadata)
        .values(
            item_id=item_id,
            timestamp=datetime.now(UTC),
            **extracted,
        )
        # Race-safety on the (item_id, timestamp) PK.
        .on_conflict_do_nothing(
            index_elements=["item_id", "timestamp"]
        )
    )
    session.execute(insert_stmt)
    return True


def _coerce_int(raw: Any) -> int | None:
    """Coerce a Pricempire wire value to an int, or None.

    Handles the three formats Pricempire mixes across endpoints:
    - native int / float (``742``, ``742.0``) → int
    - numeric string (``"742"``) → int
    - None / missing / non-numeric (``"abc"``, ``""``) → None

    Floats are truncated via ``int()``; Pricempire's int-shaped fields
    don't have fractional parts in practice (you can't have 2.5
    trades), but the truncation is the right move if one ever shows
    up. ``False`` is treated as a non-numeric, not 0 — Pricempire
    doesn't return booleans for these fields, and silently coercing
    one would mask a wire-format change.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        try:
            return int(raw)
        except (ValueError, OverflowError):
            return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            # Try float-then-int for "742.0"-style strings.
            try:
                return int(float(s))
            except (ValueError, OverflowError):
                return None
    return None


def _coerce_decimal(raw: Any) -> Decimal | None:
    """Coerce Pricempire's ``liquidity`` (0-100 float) to a Decimal
    fitting NUMERIC(6,2). None / malformed → None.

    Quantize to two decimals so the dedup-tuple comparison is stable
    (otherwise a wire float of 62.8025... would never match a stored
    62.80 and we'd write every cycle)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    except Exception:
        return None


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
