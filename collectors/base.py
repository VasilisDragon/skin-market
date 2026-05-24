"""Base building blocks shared by all marketplace collectors.

A collector's job is to fetch raw price data from one upstream API, normalize
it into a ``PriceObservation``, and hand it to ``persist_observation``. The
base layer provides:

- ``PriceObservation``: the dataclass that crosses the collector/DB boundary.
- ``full_jitter_backoff``: an AWS-style backoff helper for 429/5xx retries.
- ``DEFAULT_USER_AGENT``: the shared Chrome-flavored UA. Steam blocks
  Python's default UA almost immediately; override per-collector if needed.
- ``Collector``: abstract base. The primary method is ``collect_cycle``,
  which fetches a list of items in one logical pass. The default
  implementation loops ``collect_one`` with ``inter_request_delay`` between
  items — this fits per-item APIs like Steam. ``SkinportCollector``
  overrides ``collect_cycle`` for its one-fetch-many-items pattern.
- ``persist_observation``: inserts a PriceObservation into the ``prices``
  table via PG ``ON CONFLICT DO NOTHING``. Shared by all collectors.

Resilience strategy (full rationale: docs/adr/006-collector-resilience.md):

- The collector itself only inserts backoff *between retries* of a failing
  call. Cycle-level pacing is the scheduler's job.
- On 429 / 5xx / network error: full-jitter exponential backoff, max 5
  attempts, then give up and return None.
- On 4xx other than 429: don't retry, return None.
- On a per-item "no listings" signal (Steam ``success:false`` /
  Skinport ``min_price=null``): return None and skip the DB write.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import Item, ObservationLog, Price, Source
from db.naming import normalize_name

logger = logging.getLogger(__name__)

# Chrome 130-ish on Windows. Updated when Steam starts demanding newer.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class PriceObservation:
    """One normalized price reading, ready for the ``prices`` table.

    ``price=None`` means "the upstream had no listings or returned an
    unparseable response" — callers should NOT persist None-priced
    observations, because that pollutes time-series averages with gaps.
    They still flow up the call stack so callers can log/count them.
    """

    market_hash_name: str
    source_name: str
    timestamp: datetime
    price: Decimal | None
    volume: int | None
    currency: str
    raw_response: dict[str, Any]


class _DeclinedMarker:
    """Sentinel: the source definitively declined to answer for this item
    (HTTP 4xx non-429, retry exhaustion on timeouts/5xx, bulk fetch error).

    Distinct from ``None`` (ambiguous — could be "no listings exist" OR
    "source soft-degraded") and ``PriceObservation`` (success). The scheduler
    counts these separately so a healthy cycle's ~10 genuinely-rare items
    (rendered as ``unavailable``) stay distinguishable from rate-limit noise
    (rendered as ``declined``). See ADR 013 for the full split rationale.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<DECLINED>"


DECLINED: _DeclinedMarker = _DeclinedMarker()


class RateLimited(Exception):
    """Raised when a collector exhausts its in-call 429 retries.

    Carries the most recent ``Retry-After`` value seen (or None if the
    upstream never sent one). The scheduler catches this from the cycle
    wrapper, computes a pause duration (header value, or a fallback
    5min→10→20→…→1h ladder), and reschedules the affected source's job.
    Other sources' jobs keep running.
    """

    def __init__(
        self, source_name: str, retry_after_seconds: int | None
    ) -> None:
        self.source_name = source_name
        self.retry_after_seconds = retry_after_seconds
        hint = (
            f"{retry_after_seconds}s"
            if retry_after_seconds is not None
            else "no header"
        )
        super().__init__(
            f"{source_name} rate-limited (Retry-After: {hint})"
        )


def parse_retry_after(header_value: str | None) -> int | None:
    """Parse an HTTP ``Retry-After`` header value into seconds.

    RFC 7231 §7.1.3 allows either a non-negative integer (delta-seconds)
    or an HTTP-date. Steam and Skinport in practice use only the integer
    form when they send the header at all; we accept that and return None
    for anything else so callers fall back to their own pause ladder.

    >>> parse_retry_after("60")
    60
    >>> parse_retry_after("0")
    0
    >>> parse_retry_after(None) is None
    True
    >>> parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    True
    >>> parse_retry_after("-5") is None
    True
    """
    if not header_value:
        return None
    try:
        seconds = int(header_value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def full_jitter_backoff(
    attempt: int, base: float = 5.0, cap: float = 300.0
) -> float:
    """AWS-style full-jitter exponential backoff.

    Returns a delay in seconds, uniformly random in
    ``[0, min(cap, base * 2**attempt)]``. The randomization spreads
    retries from many clients so they don't synchronize and DDoS the
    upstream on its recovery.

    Args:
        attempt: 0-indexed retry attempt count.
        base: starting delay (seconds) at attempt=0's upper bound.
        cap: hard ceiling (seconds) on the delay.
    """
    upper = min(cap, base * (2**attempt))
    return random.uniform(0, upper)


class Collector(ABC):
    """Abstract base for a single-source marketplace collector."""

    source_name: str
    user_agent: str = DEFAULT_USER_AGENT
    # Delay between successive items in the default ``collect_cycle`` loop.
    # Subclasses that override ``collect_cycle`` (e.g. Skinport's bulk
    # fetch) can leave this at 0.
    inter_request_delay: float = 0.0

    @abstractmethod
    def collect_one(
        self, client: httpx.Client, market_hash_name: str
    ) -> PriceObservation | _DeclinedMarker | None:
        """Fetch and normalize a single item.

        Returns one of three signals:

        - ``PriceObservation``: upstream returned usable data.
        - ``DECLINED``: source refused to answer (4xx non-429, timeout/5xx
          retry exhaustion). Counted distinct from ambiguous "no listings".
        - ``None``: ambiguous — success-shape response with no parseable
          price (Steam ``success:false``, Skinport ``min_price:null``,
          DMarket empty ``objects[]``). The scheduler applies a cycle-level
          heuristic to re-label these as ``declined`` when an outsized
          fraction of the cycle came back empty (soft-degrade detection).

        Raises ``RateLimited`` when 429 retries exhaust — aborts the cycle
        and lets the scheduler pause the source's APScheduler job. Must
        not raise on any other failure mode.
        """

    def collect_cycle(
        self,
        client: httpx.Client,
        market_hash_names: Iterable[str],
    ) -> Iterator[PriceObservation | _DeclinedMarker | None]:
        """Yield one signal per name for the whole watchlist.

        Default implementation: serial per-item fetch via ``collect_one``,
        with ``inter_request_delay`` seconds between successive items.
        Suits APIs that expose a per-item endpoint, e.g. Steam's
        priceoverview.

        Subclasses with a bulk endpoint (Skinport) override this and yield
        signals after one HTTP call.

        Propagates ``RateLimited`` if ``collect_one`` raises — the cycle
        aborts mid-iteration and the scheduler handles the pause.
        """
        names = list(market_hash_names)
        for i, name in enumerate(names):
            if i > 0 and self.inter_request_delay > 0:
                time.sleep(self.inter_request_delay)
            yield self.collect_one(client, name)

    def make_client(self) -> httpx.Client:
        """Construct the httpx client used for this collector's requests.

        Default settings: Chrome UA, JSON accept, no redirects (Steam's
        priceoverview never redirects; following a redirect is suspicious),
        sensible timeouts.

        Subclasses can override for source-specific quirks. A future cookie
        layer for Steam will plug in here — see ADR 006 (cookie/session
        strategy) for the plan.
        """
        return httpx.Client(
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                connect=10.0, read=30.0, write=10.0, pool=10.0
            ),
            follow_redirects=False,
        )


def update_observation_log(
    session: Session, obs: PriceObservation
) -> bool:
    """Record that a source successfully observed an item at
    ``obs.timestamp``, regardless of whether ``persist_observation``
    will write a ``prices`` row (i.e. independent of dedup).

    The streak counter (``analytics.unavailability_streak``) reads
    from ``observation_log`` so dedup'd observations correctly count
    as "fresh" rather than as missing. ADR 015 §"unavailability_streak"
    for the rationale.

    Returns True if the upsert ran, False if the item or source name
    is missing from the DB (defensive — matches ``persist_observation``'s
    same lookup-failed exit).
    """
    canonical_name = normalize_name(obs.market_hash_name)
    item_id = session.execute(
        select(Item.id).where(Item.market_hash_name == canonical_name)
    ).scalar_one_or_none()
    if item_id is None:
        return False
    source_id = session.execute(
        select(Source.id).where(Source.name == obs.source_name)
    ).scalar_one_or_none()
    if source_id is None:
        return False

    stmt = (
        pg_insert(ObservationLog)
        .values(
            item_id=item_id,
            source_id=source_id,
            last_observed_at=obs.timestamp,
        )
        .on_conflict_do_update(
            index_elements=["item_id", "source_id"],
            set_={"last_observed_at": obs.timestamp},
        )
    )
    session.execute(stmt)
    return True


def persist_observation(session: Session, obs: PriceObservation) -> bool:
    """Insert a PriceObservation into ``prices``.

    Looks up ``item_id`` by NFC-normalized ``market_hash_name`` and
    ``source_id`` by ``source_name``. Returns True if a row was written
    (or already existed at the same composite PK); False if the item or
    source is unknown, or if the observation had no price to write.

    Uses Postgres' ``INSERT ... ON CONFLICT DO NOTHING`` so racing collectors
    or accidental same-timestamp writes are silent no-ops, not errors.
    """
    if obs.price is None:
        logger.info(
            "Skipping persist for %r — price is None", obs.market_hash_name
        )
        return False

    canonical_name = normalize_name(obs.market_hash_name)
    item_id = session.execute(
        select(Item.id).where(Item.market_hash_name == canonical_name)
    ).scalar_one_or_none()
    if item_id is None:
        logger.warning(
            "Item %r not in watchlist — not persisting",
            obs.market_hash_name,
        )
        return False

    source_id = session.execute(
        select(Source.id).where(Source.name == obs.source_name)
    ).scalar_one_or_none()
    if source_id is None:
        logger.error(
            "Source %r not seeded — not persisting", obs.source_name
        )
        return False

    stmt = (
        pg_insert(Price)
        .values(
            item_id=item_id,
            source_id=source_id,
            timestamp=obs.timestamp,
            price=obs.price,
            volume=obs.volume,
            currency=obs.currency,
            raw_response=obs.raw_response,
        )
        .on_conflict_do_nothing(
            index_elements=["item_id", "source_id", "timestamp"]
        )
    )
    session.execute(stmt)
    return True


def should_write_observation(
    session: Session, obs: PriceObservation
) -> bool:
    """Return True if this observation should be persisted.

    Returns False when the most recent row for the same ``(item, source)``
    has the same ``(price, volume)`` — an unchanged observation adds noise
    to the time-series without new information. **Exact equality**, no
    tolerance threshold (see ADR 009 for the rationale: tolerances are an
    arbitrary bug source; cent-level changes are real signal).

    Always True if no prior row exists. Always False if the item or source
    is unknown (defensive — ``persist_observation`` also handles that,
    and we want this function to return early without writing).

    One SQL round-trip per call: a single JOIN-based SELECT bounded by
    LIMIT 1 hits the composite PK index on ``prices``. At v1 cadences
    (48 items × 2 sources × 12 cycles/hour) this is ~1100 lookups/hour,
    well within Postgres's noise floor.
    """
    if obs.price is None:
        return False

    canonical_name = normalize_name(obs.market_hash_name)

    latest = session.execute(
        select(Price.price, Price.volume)
        .join(Item, Item.id == Price.item_id)
        .join(Source, Source.id == Price.source_id)
        .where(
            Item.market_hash_name == canonical_name,
            Source.name == obs.source_name,
        )
        .order_by(Price.timestamp.desc())
        .limit(1)
    ).first()

    if latest is None:
        return True
    return (latest.price, latest.volume) != (obs.price, obs.volume)
