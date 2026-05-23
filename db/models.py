"""SQLAlchemy ORM models for the skin-market schema.

Four tables:

- ``items``: one row per CS2 item we track. UUID PK so the slug can change
  without breaking foreign keys.
- ``sources``: one row per upstream marketplace API. Small lookup table.
- ``prices``: composite PK (item_id, source_id, timestamp). Converted to a
  TimescaleDB hypertable in the initial migration. ``raw_response`` keeps
  the upstream JSON for replay/debugging.
- ``insights``: derived analytics (moving averages, anomaly flags, etc.).
  Written by the analytics layer, read by the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    market_hash_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    item_type: Mapped[str | None] = mapped_column(Text)
    weapon_name: Mapped[str | None] = mapped_column(Text)
    skin_name: Mapped[str | None] = mapped_column(Text)
    wear: Mapped[str | None] = mapped_column(Text)
    is_stattrak: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_souvenir: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    base_url: Mapped[str | None] = mapped_column(Text)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # 'usd' (real-money) vs 'wallet_credit' (Steam Wallet) vs future
    # 'rmb', 'eth', etc. Used by the analytics + narrative formatters
    # to label prices honestly. See docs/sources-and-semantics.md.
    denomination: Mapped[str | None] = mapped_column(Text)
    # Scheduler reads these at startup to register one APScheduler job
    # per enabled source. Scalar columns rather than a JSONB policy blob
    # because today's knobs are scalar (ADR 013 §1). Migrate to JSONB if
    # we ever need structured policy (e.g. per-status-code backoff
    # curves, conditional rules).
    #
    # Server defaults give fresh inserts (seed_watchlist, ad-hoc test
    # sources) a conservative cadence without forcing every caller to
    # know about these columns. Production sources have explicit values
    # set by migration 0003.
    interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )
    per_item_delay_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )


class Price(Base):
    __tablename__ = "prices"

    # Composite PK. ``timestamp`` is included so TimescaleDB can use it as
    # the partitioning column (a unique constraint must contain the
    # partition key).
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id"), primary_key=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    volume: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="USD"
    )
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ObservationLog(Base):
    """One row per ``(item, source)`` pair — the timestamp of the most
    recent successful poll, regardless of whether a ``prices`` row was
    written.

    Phase 7a addition. Distinct from ``prices``:

    - ``prices`` is the dedup'd time-series — rows only land when
      ``(price, volume)`` changes (ADR 009 §3). For items whose price
      doesn't change cycle-to-cycle, ``prices.timestamp`` stops
      advancing even though the collector is still observing them.
    - ``observation_log`` advances on every successful observation
      (pre-dedup), so it's the right signal for "has source X seen
      item Y recently?" — used by
      ``analytics.unavailability_streak``.

    Composite PK ``(item_id, source_id)`` means there is exactly one
    row per pair; the collector upserts via
    ``INSERT … ON CONFLICT DO UPDATE``.
    """

    __tablename__ = "observation_log"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id"), primary_key=True
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PricempireObservation(Base):
    """One Pricempire sub-provider's reading for one item at one cycle.

    Phase 2a (ADR 018/019). Layered on top of the existing curated
    Steam/Skinport/DMarket collectors — Pricempire is breadth coverage
    across buff163/skinport/dmarket/csmoney/swap.gg/buff163_buy.

    Three timestamps live here on purpose (see migration 0005's
    docstring):

    - ``timestamp`` — local clock at row-write time. Drives dedup and
      TimescaleDB chunking. Project-canonical.
    - ``last_checked_at`` — Pricempire's claim of when it last polled
      the provider. Informational; Phase 2b drift signal.
    - ``updated_at`` — Pricempire's claim of when the underlying
      price last moved. Informational. Note: Skinport rows carry a
      placeholder 2025-01-01 here in practice — handle gracefully.

    Pricempire wire prices are in cents (e.g. 17316 = $173.16); the
    collector divides by 100 before insert.
    """

    __tablename__ = "pricempire_observations"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id"), primary_key=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        server_default=func.now(),
    )
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    count: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="USD"
    )
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class PricempireObservationLog(Base):
    """Per-(item, source) Pricempire-poll-time signal, advanced
    unconditionally on every successful wire-row parse — BEFORE the
    dedup gate suppresses an unchanged-row write.

    Phase 2b addition (ADR 023). Mirrors the existing ObservationLog
    table for the curated collectors. The drift detector reads
    last_observed_at to gate stale comparisons; without this table,
    Pricempire's dedup-suppressed cycles would silently age the only
    available freshness signal (raw_response->>'last_checked_at' from
    the latest written row), reproducing Phase 1's observed_at bug
    one level up.

    Composite PK (item_id, source_id) — one row per pair, upserted via
    INSERT ... ON CONFLICT DO UPDATE. Regular table (NOT hypertable):
    cardinality is bounded by curated-tier items × Pricempire sub-
    providers (~42 × 6 ≈ 252 rows total in v1).

    The collector helper collectors.pricempire._upsert_observation_log
    MUST be called BEFORE the dedup gate's SELECT. Test
    tests/test_pricempire_collector.py::test_observation_log_upserts_before_dedup
    pins this invariant.
    """

    __tablename__ = "pricempire_observation_log"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id"), primary_key=True
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PricempireItemMetadata(Base):
    """Per-item slow-changing metadata Pricempire returns alongside
    prices: rank, liquidity, marketcap, Steam-side trade volumes.

    Phase 2a follow-up to ADR 018/019; ADR 020 documents the
    side-effect-of-price-ingest pattern. Lifted out of
    ``pricempire_observations.raw_response`` JSONB into typed columns
    so they're queryable / indexable. The dedup gate compares the
    full metadata tuple against the most-recent existing row; most
    cycles write zero rows because rank/liquidity/etc. shift slowly.

    ``steam_last_24h`` is reserved for a future Pricempire ``/metas``
    cron — the Phase 2a collector reads only ``/prices``, which does
    not carry that field, so the column is always NULL today.
    """

    __tablename__ = "pricempire_item_metadata"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id"), primary_key=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        server_default=func.now(),
    )
    rank: Mapped[int | None] = mapped_column(Integer)
    liquidity: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    marketcap: Mapped[int | None] = mapped_column(BigInteger)
    count: Mapped[int | None] = mapped_column(Integer)
    trades_7d: Mapped[int | None] = mapped_column(Integer)
    trades_30d: Mapped[int | None] = mapped_column(Integer)
    trades_90d: Mapped[int | None] = mapped_column(Integer)
    steam_last_24h: Mapped[int | None] = mapped_column(Integer)
    steam_last_7d: Mapped[int | None] = mapped_column(Integer)
    steam_last_30d: Mapped[int | None] = mapped_column(Integer)
    steam_last_90d: Mapped[int | None] = mapped_column(Integer)


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id"), nullable=False
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    insight_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    # Used by insight_type='daily_narrative' to store an English
    # paragraph (ADR 007). NULL for all numeric insight types.
    text_value: Mapped[str | None] = mapped_column(Text)
    # Named ``meta_info`` rather than ``metadata`` because the latter is a
    # reserved attribute name on SQLAlchemy's DeclarativeBase — anyone who
    # writes ``insight.metadata = {...}`` would silently reassign the
    # registry instead of setting a column value.
    meta_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class LLMUsageLog(Base):
    """One row per DeepSeek API request.

    Stores the model-reported token counts, price rates in effect at
    request time, computed request cost, and a bounded prompt audit
    fingerprint. ``full_prompt`` stays NULL unless an explicit dev flag
    enables full prompt retention.
    """

    __tablename__ = "llm_usage_log"

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    discord_user_id: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_cache_hit_tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_cache_miss_tokens: Mapped[int | None] = mapped_column(Integer)
    input_cache_hit_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), nullable=False
    )
    input_cache_miss_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), nullable=False
    )
    output_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), nullable=False
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_preview: Mapped[str] = mapped_column(Text, nullable=False)
    full_prompt: Mapped[str | None] = mapped_column(Text)
    raw_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class PriceAlert(Base):
    """Persistent Discord-owned price alert subscription."""

    __tablename__ = "price_alerts"
    __table_args__ = (
        CheckConstraint(
            "currency IN ('usd', 'wallet_credit')",
            name="ck_price_alerts_currency",
        ),
        CheckConstraint(
            "alert_mode IN ('price_threshold', 'percent_move')",
            name="ck_price_alerts_alert_mode",
        ),
        CheckConstraint(
            "direction IN ('at_or_below', 'at_or_above')",
            name="ck_price_alerts_direction",
        ),
        CheckConstraint(
            "threshold_pct IS NULL OR threshold_pct > 0",
            name="ck_price_alerts_threshold_pct",
        ),
        CheckConstraint(
            "status IN ('active', 'triggered', 'cancelled')",
            name="ck_price_alerts_status",
        ),
        CheckConstraint(
            "quiet_start_hour IS NULL OR "
            "(quiet_start_hour >= 0 AND quiet_start_hour <= 23)",
            name="ck_price_alerts_quiet_start",
        ),
        CheckConstraint(
            "quiet_end_hour IS NULL OR "
            "(quiet_end_hour >= 0 AND quiet_end_hour <= 23)",
            name="ck_price_alerts_quiet_end",
        ),
        CheckConstraint(
            "timezone_offset_minutes >= -720 AND timezone_offset_minutes <= 840",
            name="ck_price_alerts_timezone_offset",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    discord_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    discord_channel_id: Mapped[str | None] = mapped_column(Text)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    slug_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    display_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    alert_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'price_threshold'")
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    threshold_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    baseline_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    baseline_source: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    trigger_source: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_delivery_error: Mapped[str | None] = mapped_column(Text)
    quiet_start_hour: Mapped[int | None] = mapped_column(Integer)
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    timezone_offset_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class PortfolioSnapshot(Base):
    """Summary-level public-inventory baseline saved for one Discord user."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'no_value_data')",
            name="ck_portfolio_snapshots_status",
        ),
        CheckConstraint(
            "currency IS NULL OR currency IN ('usd')",
            name="ck_portfolio_snapshots_currency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    discord_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    steam_id: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str | None] = mapped_column(Text)
    baseline_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    baseline_mid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    baseline_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    priced_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    unpriced_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    stickered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    top_item_share_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    portfolio_baseline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    top_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    largest_spread_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    unpriced_sample: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class DiscordEntitlement(Base):
    """Operator-managed Discord tier/status row for quota enforcement."""

    __tablename__ = "discord_entitlements"
    __table_args__ = (
        CheckConstraint(
            "tier IN ('free', 'trader', 'pro')",
            name="ck_discord_entitlements_tier",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_discord_entitlements_status",
        ),
    )

    discord_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )


class SignalSubscription(Base):
    """Recurring Discord delivery settings for ranked market signal digests."""

    __tablename__ = "signal_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "hours >= 1 AND hours <= 24",
            name="ck_signal_subscriptions_hours",
        ),
        CheckConstraint(
            '"limit" >= 1 AND "limit" <= 20',
            name="ck_signal_subscriptions_limit",
        ),
        CheckConstraint(
            "threshold_z >= 0",
            name="ck_signal_subscriptions_threshold_z",
        ),
        CheckConstraint(
            "lane IN ('all', 'market_movers', 'spread_watch')",
            name="ck_signal_subscriptions_lane",
        ),
        CheckConstraint(
            "interval_minutes >= 15 AND interval_minutes <= 10080",
            name="ck_signal_subscriptions_interval",
        ),
        CheckConstraint(
            "quiet_start_hour IS NULL OR "
            "(quiet_start_hour >= 0 AND quiet_start_hour <= 23)",
            name="ck_signal_subscriptions_quiet_start",
        ),
        CheckConstraint(
            "quiet_end_hour IS NULL OR "
            "(quiet_end_hour >= 0 AND quiet_end_hour <= 23)",
            name="ck_signal_subscriptions_quiet_end",
        ),
        CheckConstraint(
            "status IN ('active', 'cancelled')",
            name="ck_signal_subscriptions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    discord_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    discord_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    lane: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'all'")
    )
    hours: Mapped[int] = mapped_column(Integer, nullable=False)
    limit: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_z: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    quiet_start_hour: Mapped[int | None] = mapped_column(Integer)
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    timezone_offset_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_delivery_error: Mapped[str | None] = mapped_column(Text)
    last_digest_fingerprint: Mapped[str | None] = mapped_column(Text)


class PortfolioMonitor(Base):
    """Recurring Discord notification settings for portfolio baseline changes."""

    __tablename__ = "portfolio_monitors"
    __table_args__ = (
        CheckConstraint(
            "interval_minutes >= 60 AND interval_minutes <= 10080",
            name="ck_portfolio_monitors_interval",
        ),
        CheckConstraint(
            "change_threshold_pct >= 0",
            name="ck_portfolio_monitors_threshold",
        ),
        CheckConstraint(
            "quiet_start_hour IS NULL OR "
            "(quiet_start_hour >= 0 AND quiet_start_hour <= 23)",
            name="ck_portfolio_monitors_quiet_start",
        ),
        CheckConstraint(
            "quiet_end_hour IS NULL OR "
            "(quiet_end_hour >= 0 AND quiet_end_hour <= 23)",
            name="ck_portfolio_monitors_quiet_end",
        ),
        CheckConstraint(
            "status IN ('active', 'cancelled')",
            name="ck_portfolio_monitors_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    discord_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    discord_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_url: Mapped[str] = mapped_column(Text, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    change_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    quiet_start_hour: Mapped[int | None] = mapped_column(Integer)
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    timezone_offset_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_delivery_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_delivery_error: Mapped[str | None] = mapped_column(Text)
