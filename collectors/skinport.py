"""Skinport bulk collector.

Endpoint: ``GET https://api.skinport.com/v1/items?app_id=730&currency=USD``

Returns a JSON array covering all ~6000 CS2 items currently on Skinport.
Each entry has the form (subset relevant to us):

    {
      "market_hash_name": "AK-47 | Redline (Field-Tested)",
      "currency": "USD",
      "min_price": 39.99,
      "max_price": 55.00,
      "mean_price": 44.20,
      "median_price": 43.00,
      "quantity": 27,
      ...
    }

Mappings to the ``prices`` table (full rationale: docs/adr/008-skinport-collector.md):

- ``prices.price``        <- ``min_price``  (lowest current listing)
- ``prices.volume``       <- ``quantity``   (current listings count, NOT 24h sales)
- ``prices.currency``     <- "USD"          (locked by query)
- ``prices.raw_response`` <- per-item slice (not the full ~6000-item dump)

When ``min_price`` is null (typically because ``quantity == 0``), we
skip-and-log, same policy as Steam's ``success:false`` per ADR 006.

The full bulk response is processed in Python: we build a name->entry map
and look up each watchlist name. Items not on the watchlist are discarded.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from collectors.base import (
    DECLINED,
    Collector,
    PriceObservation,
    RateLimited,
    _DeclinedMarker,
    full_jitter_backoff,
    parse_retry_after,
    persist_observation,
)
from db.connection import get_engine
from db.models import Item
from db.naming import normalize_name

logger = logging.getLogger(__name__)


SKINPORT_BASE_URL = "https://api.skinport.com/v1/items"
SKINPORT_APP_ID_CS2 = 730


def parse_skinport_price(
    value: float | int | str | None,
) -> Decimal | None:
    """Convert a Skinport numeric price to Decimal.

    The API returns JSON numbers (parsed as float by json.loads). Round-trip
    via ``str()`` preserves the printable representation:
    ``42.5`` -> ``"42.5"`` -> ``Decimal("42.5")``. Going directly through
    ``Decimal(float)`` would inject float imprecision —
    ``Decimal(42.5)`` is actually ``Decimal("42.5000000000000071...")``,
    which silently writes 18 ulps of nonsense into NUMERIC(12,2).
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class SkinportCollector(Collector):
    """One-fetch-many-items collector for Skinport's /v1/items endpoint."""

    source_name = "skinport"
    base_delay: float = 5.0
    max_retries: int = 5
    # ``collect_cycle`` is overridden, so the base's inter-request loop
    # is never used. Leave at 0 for clarity.
    inter_request_delay: float = 0.0

    def make_client(self) -> httpx.Client:
        """Override the base client to add ``Accept-Encoding: br, gzip``.

        Skinport's API returns ``406 Not Acceptable`` for any request that
        does not advertise brotli (``br``) in Accept-Encoding. The
        ``brotli`` package is a runtime dependency so httpx decompresses
        the response transparently.
        """
        client = super().make_client()
        client.headers["Accept-Encoding"] = "br, gzip"
        return client

    def collect_one(
        self, client: httpx.Client, market_hash_name: str
    ) -> PriceObservation | _DeclinedMarker | None:
        """Convenience: fetch the bulk response and filter to one item.

        Wasteful for production use (the scheduler should call
        ``collect_cycle`` instead) but satisfies the abstract API and
        keeps a useful debug path.
        """
        for obs in self.collect_cycle(client, [market_hash_name]):
            return obs
        return None

    def collect_cycle(
        self,
        client: httpx.Client,
        market_hash_names: Iterable[str],
    ) -> Iterator[PriceObservation | _DeclinedMarker | None]:
        wanted = [normalize_name(n) for n in market_hash_names]
        # ``_fetch_bulk`` may raise RateLimited if 429 retries exhaust.
        # We let it propagate — the scheduler will pause Skinport's job.
        response_index = self._fetch_bulk(client)

        if response_index is None:
            # Bulk fetch failed on a non-429 path (parse error, HTTP 4xx
            # non-429, timeout exhaustion). All items are declined — same
            # outcome for every name in this cycle.
            for _ in wanted:
                yield DECLINED
            return

        # Single timestamp for the whole cycle — the response is a server
        # snapshot, not a per-item reading. The composite PK
        # (item_id, source_id, timestamp) keeps each item distinct.
        timestamp = datetime.now(UTC)
        for name in wanted:
            entry = response_index.get(name)
            if entry is None:
                # Item not in Skinport's bulk response at all. Ambiguous:
                # could be "Skinport doesn't list this item" (genuine
                # unavailable) or a degraded subset; cycle-level heuristic
                # decides.
                logger.info(
                    "Skinport has no entry for %r — skipping", name
                )
                yield None
                continue
            yield self._normalize(name, entry, timestamp)

    def _fetch_bulk(
        self, client: httpx.Client
    ) -> dict[str, dict] | None:
        """Fetch the full Skinport item list once, return a name->entry map.

        The map key is the NFC-normalized ``market_hash_name`` so lookups
        against our watchlist (also NFC-normalized) match exactly. Returns
        None on retry exhaustion, non-200 status, or unparseable body so
        the cycle can degrade gracefully (yields Nones, no DB writes).
        """
        params = {
            "app_id": str(SKINPORT_APP_ID_CS2),
            "currency": "USD",
        }

        saw_429 = False
        last_retry_after_seconds: int | None = None

        for attempt in range(self.max_retries):
            try:
                response = client.get(SKINPORT_BASE_URL, params=params)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "Skinport timeout (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                logger.warning(
                    "Skinport HTTP error (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                self._sleep_backoff(attempt)
                continue

            status = response.status_code
            if status == 429:
                saw_429 = True
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After")
                )
                if retry_after is not None:
                    last_retry_after_seconds = retry_after
                    delay = float(min(retry_after, 60))
                else:
                    delay = full_jitter_backoff(
                        attempt, base=self.base_delay
                    )
                logger.warning(
                    "Skinport 429 (attempt %d/%d) — "
                    "Retry-After=%s, sleeping %.2fs",
                    attempt + 1,
                    self.max_retries,
                    retry_after if retry_after is not None else "absent",
                    delay,
                )
                time.sleep(delay)
                continue

            if status >= 500:
                logger.warning(
                    "Skinport %d (attempt %d/%d) — backing off",
                    status,
                    attempt + 1,
                    self.max_retries,
                )
                self._sleep_backoff(attempt)
                continue

            if status != 200:
                logger.warning("Skinport %d — not retrying", status)
                return None

            try:
                body = response.json()
            except ValueError:
                logger.warning("Skinport returned non-JSON body")
                return None

            if not isinstance(body, list):
                logger.warning(
                    "Skinport returned non-list body: %s",
                    type(body).__name__,
                )
                return None

            return {
                normalize_name(entry["market_hash_name"]): entry
                for entry in body
                if isinstance(entry, dict) and entry.get("market_hash_name")
            }

        logger.error(
            "Skinport bulk fetch exhausted %d attempts", self.max_retries
        )
        if saw_429:
            raise RateLimited(self.source_name, last_retry_after_seconds)
        return None

    def _normalize(
        self,
        canonical_name: str,
        entry: dict,
        timestamp: datetime,
    ) -> PriceObservation | None:
        price = parse_skinport_price(entry.get("min_price"))
        if price is None:
            logger.info(
                "Skinport min_price is null for %r — skipping",
                canonical_name,
            )
            return None
        quantity = entry.get("quantity")
        volume = int(quantity) if isinstance(quantity, int) else None
        return PriceObservation(
            market_hash_name=canonical_name,
            source_name=self.source_name,
            timestamp=timestamp,
            price=price,
            volume=volume,
            currency="USD",
            raw_response=entry,
        )

    def _sleep_backoff(self, attempt: int) -> None:
        delay = full_jitter_backoff(attempt, base=self.base_delay)
        logger.info("Skinport backoff %.2fs before retry", delay)
        time.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Skinport collector once over the current watchlist."
        )
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format=(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"name":"%(name)s","msg":%(message)r}'
        ),
    )

    collector = SkinportCollector()
    engine = get_engine()

    # Pull the watchlist from the DB so the collector matches whatever
    # state the seed left. Phase 4's scheduler will pass an explicit list.
    with Session(engine) as session:
        watchlist = [
            row[0]
            for row in session.execute(select(Item.market_hash_name)).all()
        ]
    logger.info(
        "Skinport cycle starting; %d watchlist items", len(watchlist)
    )

    with collector.make_client() as client:
        observations = list(collector.collect_cycle(client, watchlist))

    written = 0
    skipped = 0
    with Session(engine) as session:
        for obs in observations:
            if obs is None:
                skipped += 1
                continue
            if persist_observation(session, obs):
                written += 1
            else:
                skipped += 1
        session.commit()

    print(
        f"Skinport cycle complete: {written} written, {skipped} skipped "
        f"(of {len(watchlist)} watchlist items)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
