"""Steam Community Market collector.

Endpoint: ``GET https://steamcommunity.com/market/priceoverview/``

Response shape (when ``success: true``):

    {
      "success": true,
      "lowest_price": "$12.34",
      "median_price": "$12.50",
      "volume": "99"
    }

The price strings are localized — for ``country=US&currency=1`` we get
``$X.XX`` with comma thousand separators (``$1,234.56``). Other locales use
different separators; this collector locks the request to US/USD so the
parser stays simple.

Failure modes and our response:

- ``success: false``: item has no current listings. INFO log, return None.
  We do NOT write a NULL price row — see ADR 006.
- HTTP 429: rate limited. Full-jitter backoff, retry up to 5 times.
- HTTP 5xx: upstream broken. Same backoff/retry.
- HTTP 4xx (not 429): bad request or unknown item. WARNING log, no retry.
- Empty body / non-JSON / dict missing ``lowest_price`` and
  ``median_price``: WARNING log, return None.
- Retry exhaustion: ERROR log, return None.

Cookies: Steam will eventually 429-block anonymous polling regardless of
backoff. The plan when that happens is to read a ``steamLoginSecure``
cookie from ``STEAM_SESSION_COOKIE`` env and add it in ``make_client``.
Not implemented in Phase 2 — see ADR 006 for the trigger condition.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from collectors.base import (
    Collector,
    PriceObservation,
    full_jitter_backoff,
)
from db.connection import get_engine
from db.models import Item, Price, Source
from db.naming import normalize_name

logger = logging.getLogger(__name__)


STEAM_BASE_URL = "https://steamcommunity.com/market/priceoverview/"
STEAM_APPID_CS2 = 730  # appid 730 covers both CS:GO and CS2
STEAM_CURRENCY_USD = 1


# Steam US prices look like "$12.34" or "$1,234.56". Strip everything except
# digits and the decimal point. Other-locale prices use comma decimals
# (e.g. "12,34€") — we don't parse those because we always request currency=1.
_PRICE_KEEP = re.compile(r"[^\d.]")
_INTEGER_KEEP = re.compile(r"[^\d]")


def parse_steam_price(s: str | None) -> Decimal | None:
    """Parse a Steam US price string into a Decimal, or None if unparseable.

    >>> parse_steam_price("$12.34")
    Decimal('12.34')
    >>> parse_steam_price("$1,234.56")
    Decimal('1234.56')
    >>> parse_steam_price("") is None
    True
    """
    if not s:
        return None
    cleaned = _PRICE_KEEP.sub("", s)
    if not cleaned or cleaned == ".":
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_steam_volume(s: str | None) -> int | None:
    """Parse a Steam volume string into an int, or None if unparseable.

    Volume comes back as a comma-separated count of recent sales, e.g.
    ``"1,234"`` for a busy item or ``"99"`` for a quiet one.
    """
    if not s:
        return None
    cleaned = _INTEGER_KEEP.sub("", s)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


class SteamCollector(Collector):
    """Collector for the Steam Community Market priceoverview endpoint."""

    source_name = "steam_market"
    base_delay: float = 5.0  # seconds; used as ``base`` for backoff
    max_retries: int = 5

    def collect_one(
        self, client: httpx.Client, market_hash_name: str
    ) -> PriceObservation | None:
        params = {
            "country": "US",
            "currency": str(STEAM_CURRENCY_USD),
            "appid": str(STEAM_APPID_CS2),
            "market_hash_name": market_hash_name,
        }

        for attempt in range(self.max_retries):
            try:
                response = client.get(STEAM_BASE_URL, params=params)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "Steam timeout (attempt %d/%d) for %r: %s",
                    attempt + 1,
                    self.max_retries,
                    market_hash_name,
                    exc,
                )
                self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                logger.warning(
                    "Steam HTTP error (attempt %d/%d) for %r: %s",
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
                    "Steam %d (attempt %d/%d) for %r — backing off",
                    status,
                    attempt + 1,
                    self.max_retries,
                    market_hash_name,
                )
                self._sleep_backoff(attempt)
                continue

            if status != 200:
                # 4xx other than 429: bad item name, malformed request, etc.
                # Not retryable.
                logger.warning(
                    "Steam %d for %r — not retrying", status, market_hash_name
                )
                return None

            obs = self._parse_response(response, market_hash_name)
            return obs

        logger.error(
            "Steam collector exhausted %d attempts for %r",
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
                "Steam returned non-JSON body for %r", market_hash_name
            )
            return None

        if not isinstance(body, dict):
            logger.warning(
                "Steam returned non-dict body for %r: %r",
                market_hash_name,
                body,
            )
            return None

        if not body.get("success"):
            logger.info(
                "Steam success:false for %r — no listings, skipping",
                market_hash_name,
            )
            return None

        price = parse_steam_price(body.get("lowest_price")) or parse_steam_price(
            body.get("median_price")
        )
        if price is None:
            logger.warning(
                "Steam success but no parseable price for %r: %r",
                market_hash_name,
                body,
            )
            return None

        return PriceObservation(
            market_hash_name=market_hash_name,
            source_name=self.source_name,
            timestamp=datetime.now(UTC),
            price=price,
            volume=parse_steam_volume(body.get("volume")),
            currency="USD",
            raw_response=body,
        )

    def _sleep_backoff(self, attempt: int) -> None:
        delay = full_jitter_backoff(attempt, base=self.base_delay)
        logger.info("Backoff %.2fs before retry", delay)
        time.sleep(delay)


def persist_observation(session: Session, obs: PriceObservation) -> bool:
    """Insert a PriceObservation into ``prices``.

    Looks up ``item_id`` by ``market_hash_name`` (NFC-normalized) and
    ``source_id`` by ``source_name``. Returns True if a row was written
    (or already existed at the same PK); False if the item or source is
    unknown.

    Skips writes for observations with ``price is None`` — those are
    "no listings" signals that don't belong in the time-series.
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
            "Item %r not in watchlist — not persisting", obs.market_hash_name
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Steam Market collector once for one item."
    )
    parser.add_argument(
        "--item",
        required=True,
        help="market_hash_name of the item to fetch (NFC; UTF-8)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default: INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format=(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"name":"%(name)s","msg":%(message)r}'
        ),
    )

    collector = SteamCollector()
    name = normalize_name(args.item)

    with collector.make_client() as client:
        obs = collector.collect_one(client, name)

    if obs is None:
        print(f"No price collected for {name!r}")
        return 1

    print(
        f"Collected: {obs.market_hash_name} = ${obs.price} "
        f"(vol {obs.volume}) at {obs.timestamp.isoformat()}"
    )
    print(f"Raw response: {json.dumps(obs.raw_response)}")

    with Session(get_engine()) as session:
        written = persist_observation(session, obs)
        session.commit()

    print(f"DB write: {'inserted' if written else 'skipped'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
