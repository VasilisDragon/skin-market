"""Base building blocks shared by all marketplace collectors.

A collector's job is to fetch raw price data from one upstream API, normalize
it into a ``PriceObservation``, and hand it to ``persist_observation`` (or its
analog). The base layer provides:

- ``PriceObservation``: the dataclass that crosses the collector/DB boundary.
- ``full_jitter_backoff``: an AWS-style backoff helper for 429/5xx retries.
- ``DEFAULT_USER_AGENT``: the shared Chrome-flavored UA. Steam blocks
  Python's default UA almost immediately; override per-collector if you need
  a different one.
- ``Collector``: an abstract base with one method, ``collect_one``. Phase 2
  has the Steam implementation; Phase 3 will add Skinport.

Resilience strategy (full rationale: docs/adr/006-collector-resilience.md):

- Per-request pacing is the responsibility of the scheduler, not the
  collector. The collector itself only inserts backoff *between retries* of
  a failing call.
- On 429 / 5xx / network error: full-jitter exponential backoff, max 5
  attempts, then give up and return None.
- On 4xx other than 429: don't retry, return None.
- On ``success:false`` from the upstream: that's a normal "no listings"
  signal, not an error. Return None and let the caller skip the write.
  See ADR 006 for why we don't write NULL price rows.
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

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

    @abstractmethod
    def collect_one(
        self, client: httpx.Client, market_hash_name: str
    ) -> PriceObservation | None:
        """Fetch and normalize a single item.

        Returns a PriceObservation when the upstream returned usable data,
        or None when the request should be skipped (no listings,
        unrecoverable HTTP error, parse failure, retry exhaustion).
        Must not raise on normal failure modes — only on programming
        errors.
        """

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
