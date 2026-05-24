"""Integration test: write a price row and read it back through the ORM.

Requires a running Postgres with the current schema applied. Skipped
automatically if the DB is unreachable (e.g. on CI without a postgres
service), so this file is safe to keep in the default test session.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from db.connection import get_engine
from db.models import Item, Price, Source


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        # Any setup failure (missing DATABASE_URL, etc.) -> skip.
        return False


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


def test_price_roundtrip() -> None:
    """Insert a price row through the ORM, query it back, assert the
    NUMERIC/JSONB/timestamp columns round-trip exactly."""
    engine = get_engine()
    with Session(engine) as session:
        # Find any existing item + source. The seed script populates these
        # in the dev flow; the test requires them to exist.
        item = session.execute(select(Item).limit(1)).scalar_one()
        source = session.execute(select(Source).limit(1)).scalar_one()

        # Use a far-future timestamp so this test doesn't clash with real
        # collector writes during dev.
        ts = datetime(2099, 1, 1, tzinfo=UTC)

        # Clean any leftover from a previous failed run.
        session.execute(
            text(
                "DELETE FROM prices WHERE item_id = :i "
                "AND source_id = :s AND timestamp = :t"
            ),
            {"i": item.id, "s": source.id, "t": ts},
        )

        price = Price(
            item_id=item.id,
            source_id=source.id,
            timestamp=ts,
            price=Decimal("12.34"),
            volume=99,
            currency="USD",
            raw_response={"lowest_price": "$12.34", "volume": "99"},
        )
        session.add(price)
        session.commit()

        round_trip = session.execute(
            select(Price).where(
                Price.item_id == item.id,
                Price.source_id == source.id,
                Price.timestamp == ts,
            )
        ).scalar_one()

        assert round_trip.price == Decimal("12.34")
        assert round_trip.volume == 99
        assert round_trip.currency == "USD"
        assert round_trip.raw_response == {
            "lowest_price": "$12.34",
            "volume": "99",
        }

        # Idempotency: clean up.
        session.delete(round_trip)
        session.commit()


def test_stattrak_market_hash_name_roundtrip() -> None:
    """The U+2122 codepoint in a StatTrak market_hash_name must survive a
    write/read roundtrip — that's the key invariant for the Steam UPSERT.

    Uses a synthetic insert/delete pattern to avoid coupling the test to
    watchlist composition.
    """
    engine = get_engine()
    synthetic_name = "StatTrak™ __Roundtrip_Sentinel__ | Test (Factory New)"
    synthetic_slug = "stattrak-roundtrip-sentinel-test-factory-new"
    with Session(engine) as session:
        # Clean any leftover from a prior failed run.
        session.execute(
            text("DELETE FROM items WHERE market_hash_name = :n"),
            {"n": synthetic_name},
        )
        session.execute(
            text(
                "INSERT INTO items "
                "(market_hash_name, display_name, slug, is_stattrak) "
                "VALUES (:n, :n, :s, true)"
            ),
            {"n": synthetic_name, "s": synthetic_slug},
        )
        session.commit()

        try:
            row = session.execute(
                text(
                    "SELECT market_hash_name FROM items "
                    "WHERE market_hash_name = :n"
                ),
                {"n": synthetic_name},
            ).scalar_one()
            assert row == synthetic_name
            # U+2122 (™) must survive byte-for-byte through Postgres.
            assert "™" in row
            assert row.encode("utf-8").startswith(b"StatTrak\xe2\x84\xa2")
        finally:
            session.execute(
                text("DELETE FROM items WHERE market_hash_name = :n"),
                {"n": synthetic_name},
            )
            session.commit()
