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
from typing import Annotated, Any, Literal

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

# Tier surfaces on every item-level response so the bot can shape its
# rendering without a follow-up call. ``curated`` items get the full
# direct-collector + Pricempire treatment (drift detection, cross-
# source spreads, etc.). ``featured`` items are Pricempire-only and
# appear in the bot's watchlist surface; the routes return Pricempire
# rows but empty direct-collector rows for them. ``substrate`` means
# the item exists in the items table but is no longer in the active
# YAML watchlist (ADR 024) — Pricempire-only observation continues
# via the bulk snapshot collector; historical curated data may exist
# from a prior tier membership. The Phase 2c rename (deep/broad/
# orphan → curated/featured/substrate) reflects post-Path-A semantics
# where the items table is bulk-populated and the YAML's tracked-list
# is the editorial overlay.
Tier = Literal["curated", "featured", "substrate"]
AlertDirection = Literal["at_or_below", "at_or_above"]
AlertStatus = Literal["active", "triggered", "cancelled"]
SignalSubscriptionStatus = Literal["active", "cancelled"]
DiscordEntitlementTier = Literal["free", "trader", "pro"]
EffectiveEntitlementTier = Literal["default", "free", "trader", "pro"]
DiscordEntitlementStatus = Literal["active", "disabled"]


class Item(BaseModel):
    """One row of the watchlist.

    weapon_name / skin_name / is_stattrak / is_souvenir surface on the
    list endpoint (not just ``ItemDetail``) so the bot can match a
    substrate slug to its sibling curated-tier wear without parsing
    ``display_name`` strings (which would be brittle on StatTrak™ /
    Souvenir / star-prefixed knives + gloves). Phase 2b Step 9.
    """

    slug: str
    market_hash_name: str
    display_name: str
    tier: Tier
    weapon_name: str | None
    skin_name: str | None
    is_stattrak: bool
    is_souvenir: bool


class ItemDetail(Item):
    """Full item metadata for ``GET /items/{slug}``."""

    item_type: str | None
    wear: str | None


class PerSourcePrice(BaseModel):
    """One source's latest reading for an item.

    Two timestamps surface to the bot, NOT one (ADR 017):

    - ``last_polled_at``: from ``observation_log.last_observed_at`` —
      the last successful poll, advanced unconditionally on every
      cycle. This is what the bot's staleness threshold reads.
    - ``last_changed_at``: from ``prices.timestamp`` — the last time
      the dedup gate (ADR 009 §3) admitted a new ``(price, volume)``
      row. Informational; "price is flat" is not a warning.

    ``last_changed_at`` is declared nullable defensively: in practice
    every ``observation_log`` row co-exists with at least one
    ``prices`` row, but the schema admits the edge case.
    """

    source: str
    denomination: Denomination
    price: MoneyStr
    volume: int | None
    last_polled_at: datetime
    last_changed_at: datetime | None


class PriceResponse(BaseModel):
    """``GET /items/{slug}/price``.

    Always returns a list — even for items only one source has. There
    is deliberately no top-level scalar ``price`` field; rendering one
    would require collapsing across denominations, which the system
    refuses to do by construction.
    """

    slug: str
    display_name: str
    tier: Tier
    sources: list[PerSourcePrice]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "display_name": "AK-47 | Redline (Field-Tested)",
                    "tier": "curated",
                    "sources": [
                        {
                            "source": "skinport",
                            "denomination": "usd",
                            "price": "28.00",
                            "volume": 27,
                            "last_polled_at": "2026-05-15T20:38:00Z",
                            "last_changed_at": "2026-05-15T04:23:00Z",
                        },
                        {
                            "source": "dmarket",
                            "denomination": "usd",
                            "price": "31.41",
                            "volume": 12,
                            "last_polled_at": "2026-05-15T20:39:00Z",
                            "last_changed_at": "2026-05-12T21:27:38Z",
                        },
                        {
                            "source": "steam_market",
                            "denomination": "wallet_credit",
                            "price": "42.92",
                            "volume": 99,
                            "last_polled_at": "2026-05-15T20:00:00Z",
                            "last_changed_at": "2026-05-15T20:00:00Z",
                        },
                    ],
                }
            ]
        }
    }


class InventoryBaselineRequest(BaseModel):
    """Request body for public-inventory asset baseline lookup."""

    inventory_url: str = Field(
        ..., description="Steam inventory item URL, including #730_2_<asset_id>."
    )


class InventorySummaryRequest(BaseModel):
    """Request body for whole public-inventory portfolio baseline lookup."""

    inventory_url: str = Field(
        ...,
        description=(
            "Steam inventory URL, optionally including a #730_2_<asset_id> "
            "fragment."
        ),
    )


class InspectBaselineRequest(BaseModel):
    """Request body for inspect-link asset baseline lookup."""

    inspect_url: str = Field(
        ..., description="CS2 inspect URL, such as steam://run/730//+..."
    )


class AssetBaselineResponse(BaseModel):
    """Structured exact-asset attribute result plus market baseline.

    ``status="ok"`` means both asset attributes and a local USD
    market-name baseline are present. ``status="no_value_data"`` means
    the exact asset was found but local price rows are missing.
    ``status="unreadable"`` covers private profiles, invalid links, and
    asset-id mismatches.
    """

    status: Literal["ok", "no_value_data", "unreadable"]
    reason: str | None
    message: str
    reference: dict[str, Any] | None
    asset: dict[str, Any] | None
    market_baseline: dict[str, Any] | None
    price_points: list[dict[str, Any]]


class InventorySummaryResponse(BaseModel):
    """Structured public-inventory portfolio market baseline result."""

    status: Literal["ok", "no_value_data", "unreadable"]
    reason: str | None
    message: str
    reference: dict[str, Any] | None
    portfolio_baseline: dict[str, Any] | None
    top_items: list[dict[str, Any]]
    largest_spread_items: list[dict[str, Any]]
    unpriced_sample: list[dict[str, Any]]


class PortfolioSnapshotCreateRequest(BaseModel):
    """Save a public-inventory portfolio baseline for one Discord user."""

    discord_user_id: str
    inventory_url: str = Field(
        ...,
        description="Steam inventory URL for the portfolio owner.",
    )


class PortfolioSnapshotResponse(BaseModel):
    """Persisted summary-level portfolio baseline snapshot."""

    id: str
    discord_user_id: str
    steam_id: str
    inventory_url: str
    status: Literal["ok", "no_value_data"]
    reason: str | None
    message: str
    created_at: datetime
    portfolio_baseline: dict[str, Any] | None
    top_items: list[dict[str, Any]]
    largest_spread_items: list[dict[str, Any]]
    unpriced_sample: list[dict[str, Any]]


class PortfolioSnapshotCreateResponse(BaseModel):
    """Snapshot creation result, including unreadable-inventory declines."""

    status: Literal["ok", "no_value_data", "unreadable"]
    message: str
    snapshot: PortfolioSnapshotResponse | None
    delta_vs_previous: dict[str, Any] | None
    summary: InventorySummaryResponse


class PortfolioSnapshotTrendResponse(BaseModel):
    """Recent persisted portfolio movement for one Discord user."""

    discord_user_id: str
    steam_id: str | None
    count: int
    latest: PortfolioSnapshotResponse | None
    previous: PortfolioSnapshotResponse | None
    delta_vs_previous: dict[str, Any] | None
    delta_since_oldest: dict[str, Any] | None
    snapshots: list[PortfolioSnapshotResponse]


class PriceAlertCreateRequest(BaseModel):
    """Create one persistent Discord-owned price alert."""

    discord_user_id: str
    discord_channel_id: str | None = None
    slug: str
    direction: AlertDirection
    threshold_price: MoneyStr
    currency: Denomination = "usd"


class PriceAlertCancelRequest(BaseModel):
    """Cancel an active alert, scoped to the requesting Discord user."""

    discord_user_id: str


class PriceAlertEvaluateRequest(BaseModel):
    """Evaluate active alerts and mark newly triggered rows."""

    limit: int = Field(default=100, ge=1, le=500)


class PriceAlertDeliveryRequest(BaseModel):
    """Record a Discord delivery attempt for one triggered alert."""

    delivered: bool
    error: str | None = Field(default=None, max_length=500)


class PriceAlertResponse(BaseModel):
    """Persistent alert row as returned by the API."""

    id: str
    discord_user_id: str
    discord_channel_id: str | None
    slug: str
    display_name: str
    direction: AlertDirection
    threshold_price: MoneyStr
    currency: Denomination
    status: AlertStatus
    created_at: datetime
    last_checked_at: datetime | None
    triggered_at: datetime | None
    trigger_price: MoneyStr | None
    trigger_source: str | None
    delivered_at: datetime | None
    delivery_attempts: int
    last_delivery_error: str | None


class PriceAlertEvaluateResponse(BaseModel):
    checked_count: int
    triggered: list[PriceAlertResponse]


class DiscordEntitlementUpdateRequest(BaseModel):
    """Operator-managed Discord entitlement state."""

    tier: DiscordEntitlementTier
    status: DiscordEntitlementStatus = "active"


class DiscordEntitlementResponse(BaseModel):
    """Effective Discord entitlement and deterministic quota policy."""

    discord_user_id: str
    tier: EffectiveEntitlementTier
    status: DiscordEntitlementStatus
    source: Literal["default", "stored"]
    created_at: datetime | None
    updated_at: datetime | None
    quotas: dict[str, int]


class SignalSubscriptionCreateRequest(BaseModel):
    """Create a recurring Discord market-signal digest subscription."""

    discord_user_id: str
    discord_channel_id: str
    hours: int = Field(default=6, ge=1, le=24)
    limit: int = Field(default=8, ge=1, le=20)
    threshold_z: MoneyStr = Field(default=Decimal("3.00"), ge=0)
    interval_minutes: int = Field(default=360, ge=15, le=10080)
    quiet_start_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_end_hour: int | None = Field(default=None, ge=0, le=23)
    timezone_offset_minutes: int = Field(default=0, ge=-720, le=840)


class SignalSubscriptionCancelRequest(BaseModel):
    """Cancel a recurring signal digest subscription for one Discord user."""

    discord_user_id: str


class SignalSubscriptionEvaluateRequest(BaseModel):
    """Evaluate due signal subscriptions for Discord delivery."""

    limit: int = Field(default=100, ge=1, le=500)


class SignalSubscriptionDeliveryRequest(BaseModel):
    """Record Discord delivery state for one signal digest subscription."""

    delivered: bool
    digest_fingerprint: str | None = Field(default=None, max_length=128)
    error: str | None = Field(default=None, max_length=500)


class SignalSubscriptionResponse(BaseModel):
    """Persisted signal digest subscription settings and delivery state."""

    id: str
    discord_user_id: str
    discord_channel_id: str
    hours: int
    limit: int
    threshold_z: MoneyStr
    interval_minutes: int
    quiet_start_hour: int | None
    quiet_end_hour: int | None
    timezone_offset_minutes: int
    status: SignalSubscriptionStatus
    created_at: datetime
    last_checked_at: datetime | None
    last_sent_at: datetime | None
    last_delivery_attempt_at: datetime | None
    delivery_attempts: int
    last_delivery_error: str | None
    last_digest_fingerprint: str | None


class SignalSubscriptionDigest(BaseModel):
    """Due subscription plus deterministic digest payload for delivery."""

    subscription: SignalSubscriptionResponse
    digest: dict[str, Any]
    digest_fingerprint: str


class SignalSubscriptionEvaluateResponse(BaseModel):
    checked_count: int
    due: list[SignalSubscriptionDigest]


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
    tier: Tier
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
                    "tier": "curated",
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
    tier: Tier
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
    has fresh polling data. These are the rows the verdict math reads.

    Two timestamps follow the ADR 017 split. ``last_polled_at`` is the
    freshness signal (from ``observation_log``) the verdict gate uses;
    ``last_changed_at`` is the timestamp of the actual price row
    (``prices.timestamp``) and is informational only — Phase 1 taught
    us that a flat-market hour leaves ``prices.timestamp`` stale even
    while the collector is polling cleanly.
    """

    source: str
    denomination: Denomination
    current: MoneyStr
    last_polled_at: datetime
    last_changed_at: datetime | None
    delta: MoneyStr
    delta_pct: str  # e.g. "+51.8%" — pre-formatted for direct render


InformationalReason = Literal["denomination_mismatch", "stale", "no_data"]


class InformationalSource(BaseModel):
    """One source not used in the verdict, with the explicit reason
    why — denomination mismatch, stale polling, or no recent observation.

    ``last_polled_at`` is the freshness signal that drove the
    ``reason="stale"`` decision (when applicable). ``last_changed_at``
    is informational; both are nullable for the no-data path.
    """

    source: str
    denomination: Denomination
    current: MoneyStr | None
    last_polled_at: datetime | None
    last_changed_at: datetime | None
    reason: InformationalReason
    note: str


class DealEvaluateResponse(BaseModel):
    slug: str
    display_name: str
    tier: Tier
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
                    "tier": "curated",
                    "offer": {"amount": "42.50", "currency": "usd"},
                    "verdict": "above_market",
                    "comparable": [
                        {
                            "source": "skinport",
                            "denomination": "usd",
                            "current": "28.00",
                            "last_polled_at": "2026-05-15T20:38:00Z",
                            "last_changed_at": "2026-05-15T04:23:00Z",
                            "delta": "14.50",
                            "delta_pct": "+51.8%",
                        }
                    ],
                    "informational": [
                        {
                            "source": "steam_market",
                            "denomination": "wallet_credit",
                            "current": "42.92",
                            "last_polled_at": "2026-05-15T20:00:00Z",
                            "last_changed_at": "2026-05-12T21:47:02Z",
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


class NarrativeResponse(BaseModel):
    """``GET /insights/narrative/latest`` — the most recent daily
    narrative paragraph plus the citation payload the LLM was given.

    The narrative is a global insight (not item-scoped); see ADR 014 §5
    for why it's NOT surfaced through ``/items/{slug}/insights``.
    """

    computed_at: datetime
    text: str
    meta: dict

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "computed_at": "2026-05-13T03:00:17Z",
                    "text": (
                        "Today, the AWP | Hyper Beast (Field-Tested) "
                        "saw a 7.07 % rise on Skinport…"
                    ),
                    "meta": {
                        "top_movers": [{"name": "AWP | Hyper Beast (Field-Tested)"}],
                        "as_of": "2026-05-13T03:00:00Z",
                    },
                }
            ]
        }
    }


AnomalyType = Literal["cross_source_divergence", "volume_anomaly"]


class AnomalyRow(BaseModel):
    """One anomaly insight, joined with the item it applies to so the
    bot can render the row without a follow-up lookup."""

    insight_type: AnomalyType
    slug: str
    display_name: str
    computed_at: datetime
    z_score: MoneyStr
    meta: dict


class AnomaliesResponse(BaseModel):
    """``GET /insights/anomalies/recent`` — cross-source divergences and
    volume anomalies from the last N hours, joined with item metadata.

    Default window is 6h, max 24h. Z-scores are signed: positive means
    the observed value is above the rolling baseline, negative is below.
    """

    since: datetime
    count: int
    anomalies: list[AnomalyRow]


class SignalDigestRow(BaseModel):
    """One ranked market signal for Discord digest rendering."""

    signal_type: AnomalyType
    slug: str
    display_name: str
    computed_at: datetime
    z_score: MoneyStr
    severity: Literal["moderate", "high", "extreme"]
    summary: str
    meta: dict


class SignalDigestResponse(BaseModel):
    """Ranked recent market signals derived from deterministic insights."""

    generated_at: datetime
    since: datetime
    hours: int
    total_anomalies: int
    returned_count: int
    signals: list[SignalDigestRow]


# Drift detector output surfaces via /items/{slug}/drift (Phase 2b
# Step 8). Verdict kinds mirror analytics/drift.py's module-level
# VERDICT_* constants; ``Classification`` mirrors the labels in
# analytics/pattern_classifier.py.
DriftVerdict = Literal[
    "drift_alert",
    "no_drift",
    "pattern_skip",
    "stale_curated",
    "stale_pricempire",
    "stale_both",
    "no_comparable_data",
]

Classification = Literal["pattern_agnostic", "phase_based", "pattern_seed"]


class DriftPairVerdict(BaseModel):
    """One pair's most-recent drift verdict.

    Up to two pairs per curated-tier item (skinport↔pricempire_skinport
    and dmarket↔pricempire_dmarket; see analytics/drift.py
    ``_MEANINGFUL_PAIRS``). Money fields use ``MoneyStr`` to keep the
    Decimal-on-wire-as-string contract; ``drift`` and
    ``threshold_used`` are likewise serialized as strings since they
    are ratios derived from money.
    """

    source_a: str
    source_b: str
    verdict: DriftVerdict
    drift: MoneyStr | None
    threshold_used: MoneyStr
    classification: Classification
    threshold_multiplier: float
    computed_at: datetime
    curated_price: MoneyStr | None
    pricempire_price: MoneyStr | None
    curated_last_polled_at: datetime | None
    pricempire_last_polled_at: datetime | None
    curated_age_min: float | None
    pricempire_age_min: float | None
    note: str | None


class DriftResponse(BaseModel):
    """``GET /items/{slug}/drift`` — most-recent verdict per meaningful
    pair for one item.

    Status-code contract:

    - 404 when ``slug`` is unknown (item not in items table).
    - 200 with ``tier="curated"``, ``pairs=[]`` when the drift detector
      hasn't produced a row yet (fresh deploy / pre-cycle).
    - 200 with ``tier="curated"``, ``pairs=[…1 or 2…]`` for items the
      detector has evaluated. One-pair shape is the realistic middle
      state for items added in Step 7.1 with sparse data for ~24h.
    - 200 with ``tier="featured"``, ``pairs=[]`` for featured-tier
      items — the detector skips them by construction (curated-only).
    - 200 with ``tier="substrate"``, ``pairs=[]`` for items in the
      items table but not in the YAML watchlist (ADR 024).
    """

    slug: str
    display_name: str
    tier: Tier
    pairs: list[DriftPairVerdict]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "slug": "ak-47-redline-field-tested",
                    "display_name": "AK-47 | Redline (Field-Tested)",
                    "tier": "curated",
                    "pairs": [
                        {
                            "source_a": "skinport",
                            "source_b": "pricempire_skinport",
                            "verdict": "no_drift",
                            "drift": "-0.0123",
                            "threshold_used": "0.10",
                            "classification": "pattern_agnostic",
                            "threshold_multiplier": 1.0,
                            "computed_at": "2026-05-17T01:30:00Z",
                            "curated_price": "28.00",
                            "pricempire_price": "28.35",
                            "curated_last_polled_at": "2026-05-17T01:28:00Z",
                            "pricempire_last_polled_at": "2026-05-17T01:29:00Z",
                            "curated_age_min": 2.0,
                            "pricempire_age_min": 1.0,
                            "note": None,
                        },
                        {
                            "source_a": "dmarket",
                            "source_b": "pricempire_dmarket",
                            "verdict": "drift_alert",
                            "drift": "0.1532",
                            "threshold_used": "0.10",
                            "classification": "pattern_agnostic",
                            "threshold_multiplier": 1.0,
                            "computed_at": "2026-05-17T01:30:00Z",
                            "curated_price": "31.41",
                            "pricempire_price": "27.24",
                            "curated_last_polled_at": "2026-05-17T01:28:00Z",
                            "pricempire_last_polled_at": "2026-05-17T01:29:00Z",
                            "curated_age_min": 2.0,
                            "pricempire_age_min": 1.0,
                            "note": None,
                        },
                    ],
                }
            ]
        }
    }

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "since": "2026-05-12T18:00:00Z",
                    "count": 2,
                    "anomalies": [
                        {
                            "insight_type": "cross_source_divergence",
                            "slug": "awp-hyper-beast-field-tested",
                            "display_name": (
                                "AWP | Hyper Beast (Field-Tested)"
                            ),
                            "computed_at": "2026-05-12T23:34:16Z",
                            "z_score": "-2.89",
                            "meta": {
                                "source_a_id": "1",
                                "source_b_id": "27",
                                "observed_spread": 0.37,
                                "baseline_mean": 0.45,
                                "baseline_stddev": 0.028,
                                "threshold_z": 2,
                                "n_samples": 21,
                            },
                        }
                    ],
                }
            ]
        }
    }
