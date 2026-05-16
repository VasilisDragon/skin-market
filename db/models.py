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
