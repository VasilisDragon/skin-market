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
- **Steam outlier filter (ADR 006 §6)**: ``lowest_price`` reflects a
  single current listing. When that listing briefly sits at a manipulated
  / fat-finger / test price (typical observation: $1.00 with volume=1),
  the cycle captures it at face value and pollutes the time series. The
  filter rejects observations below ``STEAM_OUTLIER_THRESHOLD_PCT`` of
  the item's 7-day median Steam price; rejected rows are treated as
  ``DECLINED`` (closest existing semantic for "source returned something
  but we refuse to persist it"). Skinport and DMarket don't need this —
  their endpoints return full listing arrays, so a single bad listing
  doesn't dominate their ``min_price`` field.

Cookies: Steam will eventually 429-block anonymous polling regardless of
backoff. The plan when that happens is to read a ``steamLoginSecure``
cookie from ``STEAM_SESSION_COOKIE`` env and add it in ``make_client``.
See ADR 006 for the trigger condition.
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
from sqlalchemy import text
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
from db.naming import normalize_name

logger = logging.getLogger(__name__)


STEAM_BASE_URL = "https://steamcommunity.com/market/priceoverview/"
STEAM_APPID_CS2 = 730  # appid 730 covers both CS:GO and CS2
STEAM_CURRENCY_USD = 1

# ─── Outlier filter (ADR 006 §6) ────────────────────────────────────────────
# Steam's lowest_price is one listing; manipulation/fat-finger/test
# listings briefly at e.g. $1.00 with volume=1 get captured at face value
# and pollute the time series. We refuse to persist observations below
# this fraction of the item's 7-day median Steam price.
#
# 0.20 calibrated against the observed contamination pattern: the
# repeated $1.00 outliers we cleaned up were ~2.3% of the median; setting
# the threshold a full order of magnitude above the worst-observed ratio
# leaves comfortable headroom for genuine moves while still catching the
# class of outlier we care about. Tunable; if a real >80% drop happens,
# revisit (or treat it as a real signal worth investigating manually).
STEAM_OUTLIER_THRESHOLD_PCT: Decimal = Decimal("0.20")

# Items with fewer than this many prior observations in the 7-day window
# fall through the filter — there isn't enough data for a meaningful
# median. Bias toward letting new-item observations land so we don't
# soft-blacklist freshly-added watchlist items.
STEAM_OUTLIER_MIN_OBSERVATIONS: int = 5

# Lookback window for the median calculation.
STEAM_OUTLIER_WINDOW_DAYS: int = 7


def _seven_day_median(market_hash_name: str) -> Decimal | None:
    """Look up the 7-day median Steam price for ``market_hash_name``.

    Opens its own DB session — collectors don't carry one (the
    ``Collector`` interface predates the outlier check). The extra
    connection per Steam cycle item is negligible at 60-min cadence.

    Returns ``None`` if there are fewer than
    ``STEAM_OUTLIER_MIN_OBSERVATIONS`` rows in the window, signalling
    "not enough history; let the observation through".
    """
    canonical = normalize_name(market_hash_name)
    with Session(get_engine()) as session:
        rows = session.execute(
            text(
                """
                SELECT p.price
                FROM prices p
                JOIN items i ON i.id = p.item_id
                JOIN sources s ON s.id = p.source_id
                WHERE i.market_hash_name = :name
                  AND s.name = 'steam_market'
                  AND p.timestamp > NOW() - make_interval(days => :days)
                """
            ),
            {
                "name": canonical,
                "days": STEAM_OUTLIER_WINDOW_DAYS,
            },
        ).all()
    if len(rows) < STEAM_OUTLIER_MIN_OBSERVATIONS:
        return None
    prices = sorted(r.price for r in rows)
    mid = len(prices) // 2
    if len(prices) % 2 == 1:
        return prices[mid]
    return (prices[mid - 1] + prices[mid]) / Decimal("2")


def _is_steam_outlier(
    median: Decimal | None, observed_price: Decimal
) -> bool:
    """Pure decision function: is ``observed_price`` low enough to
    reject as an outlier given the recent ``median``?

    ``median is None`` → False (no median, no filter; new-item path).
    Strict less-than at the threshold — observations exactly at
    ``median * THRESHOLD_PCT`` pass (the threshold is the floor, not
    a forbidden value).
    """
    if median is None:
        return False
    threshold = median * STEAM_OUTLIER_THRESHOLD_PCT
    return observed_price < threshold


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
    # 5 seconds between successive item fetches when ``collect_cycle`` is
    # used by the scheduler. Conservative; Steam tolerates this indefinitely.
    inter_request_delay: float = 5.0
    # Same value used as the base for full-jitter backoff on 429/5xx.
    base_delay: float = 5.0
    max_retries: int = 5

    def collect_one(
        self, client: httpx.Client, market_hash_name: str
    ) -> PriceObservation | _DeclinedMarker | None:
        params = {
            "country": "US",
            "currency": str(STEAM_CURRENCY_USD),
            "appid": str(STEAM_APPID_CS2),
            "market_hash_name": market_hash_name,
        }

        # Track 429s so retry exhaustion raises RateLimited (with the most
        # recent Retry-After hint, if any). Steam's 429 body is literally
        # `null` and the header is usually absent, but if it ever shows up
        # we honor it.
        saw_429 = False
        last_retry_after_seconds: int | None = None

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

            if status == 429:
                saw_429 = True
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After")
                )
                if retry_after is not None:
                    last_retry_after_seconds = retry_after
                    # Cap the in-call sleep at 60s — past that we're better
                    # off propagating to the scheduler so the whole job
                    # pauses, not blocking this cycle in a long sleep.
                    delay = float(min(retry_after, 60))
                else:
                    delay = full_jitter_backoff(
                        attempt, base=self.base_delay
                    )
                logger.warning(
                    "Steam 429 (attempt %d/%d) for %r — "
                    "Retry-After=%s, sleeping %.2fs",
                    attempt + 1,
                    self.max_retries,
                    market_hash_name,
                    retry_after if retry_after is not None else "absent",
                    delay,
                )
                time.sleep(delay)
                continue

            if status >= 500:
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
                # Not retryable, definitive decline.
                logger.warning(
                    "Steam %d for %r — not retrying", status, market_hash_name
                )
                return DECLINED

            obs = self._parse_response(response, market_hash_name)
            if obs is None:
                return None

            # Steam outlier filter — ADR 006 §6. priceoverview's
            # lowest_price is one listing; manipulation moments at
            # e.g. $1 with volume=1 get captured at face value if we
            # don't refuse them at the collector layer. Check against
            # the 7-day median; treat outliers as DECLINED so the
            # cycle counter surfaces them without poisoning prices.
            median = _seven_day_median(obs.market_hash_name)
            if _is_steam_outlier(median, obs.price):
                threshold = median * STEAM_OUTLIER_THRESHOLD_PCT
                logger.warning(
                    "Steam outlier filter: rejecting %r at price=%s "
                    "(7-day median=%s, threshold=%s, volume=%s) — "
                    "treating as DECLINED",
                    obs.market_hash_name,
                    obs.price,
                    median,
                    threshold,
                    obs.volume,
                )
                return DECLINED
            return obs

        logger.error(
            "Steam collector exhausted %d attempts for %r",
            self.max_retries,
            market_hash_name,
        )
        if saw_429:
            # Abort the cycle and let the scheduler pause Steam's job.
            raise RateLimited(self.source_name, last_retry_after_seconds)
        # Exhausted on timeouts / 5xx — source is unreachable for this
        # item; count as declined rather than ambiguous-unavailable.
        return DECLINED

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
