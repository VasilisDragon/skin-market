"""Pydantic v2 schemas for the read-only API.

Two hard rules these models exist to enforce:

1. **Money is serialized as a string in JSON**, not a float. ``Decimal``
   in Python; ``"42.50"`` on the wire. ``MoneyStr`` is the
   ``Annotated[Decimal, PlainSerializer]`` alias used everywhere a
   price/value crosses the boundary. ADR 014 §1 has the rationale —
   in short, float-in-JSON loses cent-level precision and
   ``NUMERIC(12, 2)`` is precise on purpose.

2. **Every price field is paired with the source's ``denomination``.**
   No top-level "price" exists; only ``(source, denomination, price)``
   tuples. This is the architectural invariant from
   ``docs/sources-and-semantics.md`` enforced at the API boundary —
   the bot cannot accidentally render "$42 on Steam" as if it were USD.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

# Decimal → str at JSON time. Pydantic v2 idiom. Field type stays
# ``Decimal`` so arithmetic inside route handlers works without
# float-cast tax; only the wire representation changes.
MoneyStr = Annotated[Decimal, PlainSerializer(str, return_type=str)]

# ``str`` literals for currency / denomination are intentionally not
# ``Enum``: adding "rmb" / "eth" / etc. is a row insert in the
# ``sources`` table (ADR 014 §2), not a code+schema change. Enums would
# require a Pydantic edit per new source.
Denomination = Literal["usd", "wallet_credit"]


class Item(BaseModel):
    """One row of the watchlist."""

    slug: str
    market_hash_name: str
    display_name: str


class ItemDetail(Item):
    """Full item metadata for ``GET /items/{slug}``."""

    item_type: str | None
    weapon_name: str | None
    skin_name: str | None
    wear: str | None
    is_stattrak: bool
    is_souvenir: bool


class PerSourcePrice(BaseModel):
    """One source's latest reading for an item.

    ``observed_at`` lets the bot answer "how fresh is this?" and feeds
    into ``/deals/evaluate``'s freshness filter (ADR 014 §4).
    """

    source: str
    denomination: Denomination
    price: MoneyStr
    volume: int | None
    observed_at: datetime


class PriceResponse(BaseModel):
    """``GET /items/{slug}/price``.

    Always returns a list — even for items only one source has. There
    is deliberately no top-level scalar ``price`` field; rendering one
    would require collapsing across denominations, which the system
    refuses to do by construction.
    """

    slug: str
    display_name: str
    sources: list[PerSourcePrice]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "display_name": "AK-47 | Redline (Field-Tested)",
                    "sources": [
                        {
                            "source": "skinport",
                            "denomination": "usd",
                            "price": "28.00",
                            "volume": 27,
                            "observed_at": "2026-05-12T21:25:06Z",
                        },
                        {
                            "source": "dmarket",
                            "denomination": "usd",
                            "price": "31.41",
                            "volume": 12,
                            "observed_at": "2026-05-12T21:27:38Z",
                        },
                        {
                            "source": "steam_market",
                            "denomination": "wallet_credit",
                            "price": "42.92",
                            "volume": 99,
                            "observed_at": "2026-05-12T21:47:02Z",
                        },
                    ],
                }
            ]
        }
    }


class HistoryObservation(BaseModel):
    timestamp: datetime
    source: str
    denomination: Denomination
    price: MoneyStr
    volume: int | None


class HistoryResponse(BaseModel):
    """``GET /items/{slug}/history`` — defaults: ``since`` = now - 7d,
    ``limit`` = 500 rows (max 5000). Keeps long-tail queries bounded —
    at Skinport's 15min cadence post-ADR-013, an active item accumulates
    ~96 rows/day even after dedup; 7d × 3 sources can hit ~2k rows.
    """

    slug: str
    source: str | None
    since: datetime
    until: datetime
    limit: int
    count: int
    observations: list[HistoryObservation]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "source": "skinport",
                    "since": "2026-05-05T00:00:00Z",
                    "until": "2026-05-12T00:00:00Z",
                    "limit": 500,
                    "count": 137,
                    "observations": [
                        {
                            "timestamp": "2026-05-11T21:25:06Z",
                            "source": "skinport",
                            "denomination": "usd",
                            "price": "28.00",
                            "volume": 27,
                        }
                    ],
                }
            ]
        }
    }


class InsightRow(BaseModel):
    """One row from the ``insights`` table, normalized for the API."""

    insight_type: str
    computed_at: datetime
    value: MoneyStr | None
    text_value: str | None
    meta: dict


class InsightsResponse(BaseModel):
    """``GET /items/{slug}/insights`` — latest of each
    (insight_type, sub-key) for one item. ``daily_narrative`` is
    excluded because it's a global insight pinned to an arbitrary
    "first item" in the schema; surfaced via a different path later
    (ADR 014 §5).
    """

    slug: str
    insights: list[InsightRow]


class Offer(BaseModel):
    """The price-and-currency the user is asking us to evaluate."""

    amount: MoneyStr
    currency: Denomination


class DealEvaluateRequest(BaseModel):
    slug: str
    offer: Offer

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "offer": {"amount": "42.50", "currency": "usd"},
                }
            ]
        }
    }


Verdict = Literal[
    "below_market",
    "at_market",
    "above_market",
    "no_comparable_data",
]


class ComparableSource(BaseModel):
    """One source whose denomination matches the offer's currency AND
    has fresh data. These are the rows the verdict math reads."""

    source: str
    denomination: Denomination
    current: MoneyStr
    observed_at: datetime
    delta: MoneyStr
    delta_pct: str  # e.g. "+51.8%" — pre-formatted for direct render


InformationalReason = Literal["denomination_mismatch", "stale", "no_data"]


class InformationalSource(BaseModel):
    """One source not used in the verdict, with the explicit reason
    why — denomination mismatch, stale data, or no recent observation."""

    source: str
    denomination: Denomination
    current: MoneyStr | None
    observed_at: datetime | None
    reason: InformationalReason
    note: str


class DealEvaluateResponse(BaseModel):
    slug: str
    display_name: str
    offer: Offer
    verdict: Verdict
    comparable: list[ComparableSource]
    informational: list[InformationalSource]
    summary: str

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "display_name": "AK-47 | Redline (Field-Tested)",
                    "offer": {"amount": "42.50", "currency": "usd"},
                    "verdict": "above_market",
                    "comparable": [
                        {
                            "source": "skinport",
                            "denomination": "usd",
                            "current": "28.00",
                            "observed_at": "2026-05-12T21:25:06Z",
                            "delta": "14.50",
                            "delta_pct": "+51.8%",
                        }
                    ],
                    "informational": [
                        {
                            "source": "steam_market",
                            "denomination": "wallet_credit",
                            "current": "42.92",
                            "observed_at": "2026-05-12T21:47:02Z",
                            "reason": "denomination_mismatch",
                            "note": (
                                "Steam Wallet credit; not directly "
                                "comparable to USD offers."
                            ),
                        }
                    ],
                    "summary": (
                        "$42.50 USD is above market for AK-47 | Redline "
                        "(Field-Tested) — Skinport listings start at $28.00."
                    ),
                }
            ]
        }
    }


class HealthResponse(BaseModel):
    status: Literal["ok"]
    db: Literal["reachable", "unreachable"] = Field(
        description="Result of a SELECT 1 against the configured DATABASE_URL."
    )
