"""DMarket collector.

Endpoint: ``GET https://api.dmarket.com/exchange/v1/market/items``

Query params:
- ``gameId=a8db``       (CS2)
- ``title=<market_hash_name>``
- ``currency=USD``
- ``limit=100``
- ``orderBy=price``
- ``orderDir=asc``

Response shape (subset relevant to us):

    {
      "objects": [
        {
          "price": {"USD": "4378"},          # stringified integer cents
          "extra": {"floatValue": 0.0712},   # per-listing float; v2 use
          "suggestedPrice": {"USD": "..."},  # DMarket's recommendation,
                                             # NOT the listing price -- ignore
          ...
        },
        ...
      ],
      "total": {...}
    }

Field mappings (full rationale: docs/adr/012-dmarket-collector.md):

- ``prices.price``        <- objects[0].price.USD / 100 (decimal dollars)
- ``prices.volume``       <- len(objects)               (stock-style listings count)
- ``prices.currency``     <- "USD"                      (locked by query)
- ``prices.raw_response`` <- cheapest offer + count; floatValue preserved
                             here for v2 work but NOT promoted to a top-
                             level column.

Failure modes — same taxonomy as Steam / Skinport (ADR 006):

- Empty ``objects`` array: no current listings. INFO log, skip. NOT a
  parse failure; this is real "no listings" signal that correlates with
  rarity (ADR 010's Phase 5 distinction applies to DMarket too).
- 429 / 5xx / timeout: full-jitter backoff, retry up to 5 times.
- 4xx non-429: WARNING log, no retry.
- Malformed body / missing fields: WARNING log, skip.

The price-vs-suggestedPrice trap is the only DMarket-specific gotcha;
ADR 012 §3 carries the details and a test guards the behavior.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from collectors.base import (
    Collector,
    PriceObservation,
    full_jitter_backoff,
    persist_observation,
)
from db.connection import get_engine
from db.naming import normalize_name

logger = logging.getLogger(__name__)


DMARKET_BASE_URL = "https://api.dmarket.com/exchange/v1/market/items"
DMARKET_GAME_ID_CS2 = "a8db"


def parse_dmarket_price(cents_str: str | int | None) -> Decimal | None:
    """Convert DMarket's stringified integer cents into Decimal dollars.

    DMarket returns ``price.USD`` as a string containing an integer
    number of cents (``"4378"`` = $43.78). We reconstruct the dollar
    Decimal via string slicing, NOT division, to keep the
    representation as ``X.YY`` regardless of trailing zeros — Decimal
    division of e.g. ``Decimal(100) / Decimal(100)`` returns
    ``Decimal('1')`` rather than ``Decimal('1.00')``, which is
    numerically equivalent but breaks equality assertions in tests.

    Examples:
        >>> parse_dmarket_price("4378")
        Decimal('43.78')
        >>> parse_dmarket_price("100")
        Decimal('1.00')
        >>> parse_dmarket_price("1")
        Decimal('0.01')
        >>> parse_dmarket_price("0")
        Decimal('0.00')
    """
    if cents_str is None or cents_str == "":
        return None
    try:
        cents = int(cents_str)
    except (ValueError, TypeError):
        return None
    if cents < 0:
        return None
    s = f"{cents:03d}"
    return Decimal(f"{s[:-2]}.{s[-2:]}")


class DMarketCollector(Collector):
    """Collector for the DMarket public market endpoint."""

    source_name = "dmarket"
    # DMarket is permissive but has no documented rate limit; mirror
    # Steam's pacing posture (conservative-but-not-paranoid) with a
    # slightly shorter delay since DMarket has no Wallet-cookie footgun.
    inter_request_delay: float = 3.0
    base_delay: float = 3.0
    max_retries: int = 5

    def collect_one(
        self, client: httpx.Client, market_hash_name: str
    ) -> PriceObservation | None:
        params = {
            "gameId": DMARKET_GAME_ID_CS2,
            "title": market_hash_name,
            "currency": "USD",
            "limit": "100",
            "orderBy": "price",
            "orderDir": "asc",
        }

        for attempt in range(self.max_retries):
            try:
                response = client.get(DMARKET_BASE_URL, params=params)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "DMarket timeout (attempt %d/%d) for %r: %s",
                    attempt + 1,
                    self.max_retries,
                    market_hash_name,
                    exc,
                )
                self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                logger.warning(
                    "DMarket HTTP error (attempt %d/%d) for %r: %s",
                    attempt + 1,
                    self.max_retries,
                    market_hash_name,
                    exc,
                )
                self._sleep_backoff(attempt)
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                logger.warning(
                    "DMarket %d (attempt %d/%d) for %r — backing off",
                    status,
                    attempt + 1,
                    self.max_retries,
                    market_hash_name,
                )
                self._sleep_backoff(attempt)
                continue

            if status != 200:
                logger.warning(
                    "DMarket %d for %r — not retrying",
                    status,
                    market_hash_name,
                )
                return None

            return self._parse_response(response, market_hash_name)

        logger.error(
            "DMarket collector exhausted %d attempts for %r",
            self.max_retries,
            market_hash_name,
        )
        return None

    def _parse_response(
        self, response: httpx.Response, market_hash_name: str
    ) -> PriceObservation | None:
        try:
            body = response.json()
        except ValueError:
            logger.warning(
                "DMarket returned non-JSON body for %r", market_hash_name
            )
            return None

        if not isinstance(body, dict):
            logger.warning(
                "DMarket returned non-dict body for %r: %s",
                market_hash_name,
                type(body).__name__,
            )
            return None

        objects = body.get("objects")
        if not isinstance(objects, list):
            logger.warning(
                "DMarket 'objects' is not a list for %r", market_hash_name
            )
            return None

        if not objects:
            # Empty objects[] = no current listings. Same policy as
            # Steam success:false and Skinport min_price:null —
            # skip-and-log, do not write a NULL price row.
            logger.info(
                "DMarket has no listings for %r — skipping", market_hash_name
            )
            return None

        cheapest = objects[0]
        if not isinstance(cheapest, dict):
            logger.warning(
                "DMarket cheapest offer not a dict for %r: %r",
                market_hash_name,
                cheapest,
            )
            return None

        # IMPORTANT: use .price.USD (actual listing) NOT .suggestedPrice
        # (DMarket's own recommendation). See ADR 012 §3.
        price_obj = cheapest.get("price")
        if not isinstance(price_obj, dict):
            logger.warning(
                "DMarket cheapest offer has no price object for %r: %r",
                market_hash_name,
                cheapest,
            )
            return None

        price = parse_dmarket_price(price_obj.get("USD"))
        if price is None:
            logger.warning(
                "DMarket cheapest offer has no parseable USD price "
                "for %r: %r",
                market_hash_name,
                price_obj,
            )
            return None

        return PriceObservation(
            market_hash_name=market_hash_name,
            source_name=self.source_name,
            timestamp=datetime.now(UTC),
            price=price,
            # Stock measurement: count of current listings at the time
            # of the snapshot. Compatible-by-meaning with Skinport's
            # quantity; NOT comparable to Steam's 24h-flow volume.
            volume=len(objects),
            currency="USD",
            raw_response={
                "cheapest": cheapest,
                "total_objects_returned": len(objects),
            },
        )

    def _sleep_backoff(self, attempt: int) -> None:
        delay = full_jitter_backoff(attempt, base=self.base_delay)
        logger.info("DMarket backoff %.2fs before retry", delay)
        time.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the DMarket collector once for one item."
    )
    parser.add_argument(
        "--item",
        required=True,
        help="market_hash_name of the item to fetch (NFC; UTF-8)",
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

    collector = DMarketCollector()
    name = normalize_name(args.item)

    with collector.make_client() as client:
        obs = collector.collect_one(client, name)

    if obs is None:
        print(f"No price collected for {name!r}")
        return 1

    print(
        f"Collected: {obs.market_hash_name} = ${obs.price} "
        f"(listings={obs.volume}) at {obs.timestamp.isoformat()}"
    )
    print(f"Raw response: {json.dumps(obs.raw_response, default=str)}")

    with Session(get_engine()) as session:
        written = persist_observation(session, obs)
        session.commit()

    print(f"DB write: {'inserted' if written else 'skipped'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
