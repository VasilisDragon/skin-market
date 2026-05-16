"""Pricempire collector tests. HTTP mocked via pytest-httpx; DB-
dependent tests skip when DATABASE_URL is unset / postgres is
unreachable, mirroring the test_api.py / test_scheduler.py pattern.

What these cover:

- Cents → dollars conversion (Pricempire wire format is cents).
- Wire-key → source-name mapping, including the swap.gg → pricempire_swap_gg
  normalization.
- Dedup gate: identical (price, count) for the latest row → "unchanged".
- Unknown items (not in our watchlist) skipped without crashing.
- Unknown provider_keys logged and skipped, not written.
- ISO timestamp parsing for Pricempire's placeholder
  (Skinport's 2025-01-01 sentinel).
- The cycle-complete log line format.

What these don't cover: streaming via ijson is exercised end-to-end
against pytest-httpx, which serves the mocked body in one chunk; the
production streaming path against the live 33MB response is verified
by Step 3's first scheduled cycle.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from collectors import pricempire
from collectors.pricempire import (
    _PROVIDER_KEY_TO_SOURCE_NAME,
    PRICEMPIRE_BASE_URL,
    PRICEMPIRE_PRICES_PATH,
    collect_snapshot,
)
from db.connection import get_engine
from db.models import Item, PricempireObservation


def _db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    except Exception:
        return False


_db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL") or not _db_reachable(),
    reason="DATABASE_URL not set or postgres unreachable",
)


_SENTINEL_NAME = "__PricempireTest__ | Sentinel (Field-Tested)"
_SENTINEL_SLUG = "pricempiretest-sentinel-field-tested"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a known fake API key so the fail-fast guard doesn't bail.
    Tests that exercise the missing-key path delenv() explicitly."""
    monkeypatch.setenv("PRICEMPIRE_API_KEY", "test-key-deadbeef")


@pytest.fixture
def sentinel_item():
    """Insert the sentinel item; yield its UUID; clean up
    pricempire_observations + the item afterward.

    Same pattern as test_api.py's sentinel_item fixture — a name no
    real CS2 item could ever match, isolated per-test cleanup so
    production data is untouched.
    """
    if not _db_reachable():
        yield None
        return
    engine = get_engine()
    item_id = uuid.uuid4()
    with Session(engine) as session:
        # Purge any leftover from a prior aborted run.
        existing = session.execute(
            select(Item.id).where(Item.market_hash_name == _SENTINEL_NAME)
        ).scalar_one_or_none()
        if existing:
            session.execute(
                text(
                    "DELETE FROM pricempire_observations "
                    "WHERE item_id = :i"
                ),
                {"i": existing},
            )
            session.execute(
                text("DELETE FROM items WHERE id = :i"), {"i": existing}
            )

        session.execute(
            text(
                """
                INSERT INTO items (
                    id, market_hash_name, display_name, slug, item_type,
                    weapon_name, skin_name, wear
                )
                VALUES (
                    :id, :name, :name, :slug, 'rifle',
                    'TestWeapon', 'Sentinel', 'Field-Tested'
                )
                """
            ),
            {
                "id": item_id,
                "name": _SENTINEL_NAME,
                "slug": _SENTINEL_SLUG,
            },
        )
        session.commit()

    yield item_id

    with Session(engine) as session:
        session.execute(
            text(
                "DELETE FROM pricempire_observations "
                "WHERE item_id = :i"
            ),
            {"i": item_id},
        )
        session.execute(
            text("DELETE FROM items WHERE id = :i"), {"i": item_id}
        )
        session.commit()


def _make_response(items: list[dict]) -> bytes:
    """Encode a list of Pricempire-shaped item dicts as JSON bytes.
    pytest-httpx serves these as the response body, and ijson streams
    from there."""
    return json.dumps(items).encode("utf-8")


def _wire_item(
    name: str,
    *,
    prices: list[dict] | None = None,
) -> dict:
    """Build a minimal Pricempire response item with the keys the
    collector reads. Other fields (liquidity, marketcap, etc.) live in
    real responses but are ignored on ingest."""
    return {
        "market_hash_name": name,
        "liquidity": 50.0,
        "rank": "100",
        "prices": prices or [],
    }


def _wire_price_row(
    *,
    provider_key: str,
    price_cents: int,
    count: int | None = 10,
    updated_at: str = "2026-05-15T20:00:00.000Z",
    last_checked_at: str = "2026-05-15T20:30:00.000Z",
) -> dict:
    return {
        "price": price_cents,
        "count": count,
        "updated_at": updated_at,
        "last_checked_at": last_checked_at,
        "provider_key": provider_key,
        "meta": None,
        "original_price": None,
        "exchange_rate": None,
    }


# ────────────────────────────────────────────────────────────────────
# Pure logic
# ────────────────────────────────────────────────────────────────────


class TestProviderKeyMapping:
    def test_all_six_providers_mapped(self) -> None:
        assert set(_PROVIDER_KEY_TO_SOURCE_NAME.values()) == {
            "pricempire_buff163",
            "pricempire_buff163_buy",
            "pricempire_skinport",
            "pricempire_dmarket",
            "pricempire_csmoney",
            "pricempire_swap_gg",
        }

    def test_swap_gg_dot_to_underscore(self) -> None:
        """Pricempire's wire key has a dot; our source name has an
        underscore. The mapping is the single place this normalization
        happens — guard against regressions."""
        assert (
            _PROVIDER_KEY_TO_SOURCE_NAME["swap.gg"]
            == "pricempire_swap_gg"
        )


class TestFailFastOnMissingKey:
    def test_missing_api_key_logs_and_returns(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        monkeypatch.delenv("PRICEMPIRE_API_KEY", raising=False)
        with caplog.at_level("ERROR", logger="collectors.pricempire"):
            collect_snapshot()
        assert any(
            "PRICEMPIRE_API_KEY is unset" in r.getMessage()
            for r in caplog.records
        )


# ────────────────────────────────────────────────────────────────────
# End-to-end (DB-dependent)
# ────────────────────────────────────────────────────────────────────


@_db_required
class TestCollectSnapshot:
    def test_cents_to_dollars_conversion(
        self, httpx_mock, sentinel_item, caplog
    ) -> None:
        """Pricempire returns 17316 cents; we persist $173.16."""
        body = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="buff163",
                            price_cents=17316,
                            count=593,
                        )
                    ],
                ),
            ]
        )
        httpx_mock.add_response(
            url=(
                f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                f"%2Cdmarket%2Cskinport%2Cswap.gg"
            ),
            content=body,
            headers={"Content-Type": "application/json"},
        )

        with caplog.at_level("INFO", logger="collectors.pricempire"):
            collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            rows = session.execute(
                select(
                    PricempireObservation.price,
                    PricempireObservation.count,
                ).where(PricempireObservation.item_id == sentinel_item)
            ).all()
        assert len(rows) == 1
        assert rows[0].price == Decimal("173.16")
        assert rows[0].count == 593

    def test_dedup_gate_skips_identical_consecutive(
        self, httpx_mock, sentinel_item
    ) -> None:
        """A second cycle with the same (price, count) writes no new
        row. Mirrors the dedup-on-write contract from ADR 009 §3."""
        body = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="skinport",
                            price_cents=2800,
                            count=27,
                        )
                    ],
                ),
            ]
        )
        # Two cycles back-to-back, same body.
        for _ in range(2):
            httpx_mock.add_response(
                url=(
                    f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                    f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                    f"%2Cdmarket%2Cskinport%2Cswap.gg"
                ),
                content=body,
                headers={"Content-Type": "application/json"},
            )

        collect_snapshot()
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            count = session.execute(
                select(PricempireObservation).where(
                    PricempireObservation.item_id == sentinel_item
                )
            ).all()
        assert len(count) == 1, (
            f"dedup should keep this at 1 row; got {len(count)}"
        )

    def test_price_change_writes_new_row(
        self, httpx_mock, sentinel_item
    ) -> None:
        """A second cycle with a different price writes a new row."""
        body_v1 = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="dmarket",
                            price_cents=3000,
                            count=12,
                        )
                    ],
                ),
            ]
        )
        body_v2 = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="dmarket",
                            price_cents=3141,  # changed
                            count=12,
                        )
                    ],
                ),
            ]
        )
        for body in (body_v1, body_v2):
            httpx_mock.add_response(
                url=(
                    f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                    f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                    f"%2Cdmarket%2Cskinport%2Cswap.gg"
                ),
                content=body,
                headers={"Content-Type": "application/json"},
            )

        collect_snapshot()
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            rows = session.execute(
                select(
                    PricempireObservation.price
                ).where(
                    PricempireObservation.item_id == sentinel_item
                ).order_by(PricempireObservation.timestamp)
            ).all()
        prices = [r.price for r in rows]
        assert prices == [Decimal("30.00"), Decimal("31.41")]

    def test_unknown_item_skipped(self, httpx_mock, caplog) -> None:
        """An item Pricempire returns that isn't in our items table is
        skipped — Phase 2a only ingests curated-watchlist items."""
        body = _make_response(
            [
                _wire_item(
                    "Pretend Item | Definitely Not Real (Factory New)",
                    prices=[
                        _wire_price_row(
                            provider_key="buff163",
                            price_cents=999,
                        )
                    ],
                ),
            ]
        )
        httpx_mock.add_response(
            url=(
                f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                f"%2Cdmarket%2Cskinport%2Cswap.gg"
            ),
            content=body,
            headers={"Content-Type": "application/json"},
        )
        with caplog.at_level("INFO", logger="collectors.pricempire"):
            collect_snapshot()
        # Find the cycle-complete log line and confirm the
        # "skipped (not in watchlist)" counter is at least 1.
        cycle_complete = next(
            (
                r.getMessage()
                for r in caplog.records
                if "cycle complete" in r.getMessage()
            ),
            None,
        )
        assert cycle_complete is not None
        assert "1 skipped (not in watchlist)" in cycle_complete

    def test_unknown_provider_skipped_and_warned(
        self, httpx_mock, sentinel_item, caplog
    ) -> None:
        """A provider_key not in _PROVIDER_KEY_TO_SOURCE_NAME doesn't
        get persisted; it's logged once per cycle."""
        body = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="buff163",
                            price_cents=1000,
                        ),
                        _wire_price_row(
                            provider_key="some_new_provider_pricempire_just_added",
                            price_cents=999,
                        ),
                    ],
                )
            ]
        )
        httpx_mock.add_response(
            url=(
                f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                f"%2Cdmarket%2Cskinport%2Cswap.gg"
            ),
            content=body,
            headers={"Content-Type": "application/json"},
        )
        with caplog.at_level("WARNING", logger="collectors.pricempire"):
            collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            rows = session.execute(
                select(PricempireObservation).where(
                    PricempireObservation.item_id == sentinel_item
                )
            ).all()
        # Only the buff163 row should land.
        assert len(rows) == 1
        # And a warning naming the unknown provider must appear.
        assert any(
            "some_new_provider_pricempire_just_added" in r.getMessage()
            for r in caplog.records
        )

    def test_skinport_2025_01_01_placeholder_parsed(
        self, httpx_mock, sentinel_item
    ) -> None:
        """Pricempire's Skinport rows carry updated_at='2025-01-01...'
        as a placeholder while last_checked_at is real. The collector
        must persist both honestly so Phase 2b drift logic can
        recognize the placeholder shape."""
        body = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="skinport",
                            price_cents=2800,
                            updated_at="2025-01-01T00:00:00.000Z",
                            last_checked_at="2026-05-15T23:51:58.798Z",
                        )
                    ],
                )
            ]
        )
        httpx_mock.add_response(
            url=(
                f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                f"%2Cdmarket%2Cskinport%2Cswap.gg"
            ),
            content=body,
            headers={"Content-Type": "application/json"},
        )
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(
                    PricempireObservation.updated_at,
                    PricempireObservation.last_checked_at,
                ).where(PricempireObservation.item_id == sentinel_item)
            ).first()
        assert row is not None
        assert row.updated_at == datetime(2025, 1, 1, tzinfo=UTC)
        assert row.last_checked_at == datetime(
            2026, 5, 15, 23, 51, 58, 798000, tzinfo=UTC
        )

    def test_non_200_exits_cleanly_no_rows(
        self, httpx_mock, sentinel_item, caplog
    ) -> None:
        """A 500 from Pricempire mid-cycle gets logged and the function
        exits; no rows written. The next scheduled cycle is the
        retry."""
        httpx_mock.add_response(
            url=(
                f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                f"%2Cdmarket%2Cskinport%2Cswap.gg"
            ),
            status_code=500,
            content=b'{"error":"upstream"}',
            headers={"Content-Type": "application/json"},
        )
        with caplog.at_level("WARNING", logger="collectors.pricempire"):
            collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            count = session.execute(
                select(PricempireObservation).where(
                    PricempireObservation.item_id == sentinel_item
                )
            ).all()
        assert count == []
        assert any(
            "500" in r.getMessage() for r in caplog.records
        ), "expected a warning logging the 500 status"


# Reference unused import to keep ruff happy if a later edit removes
# the only consumer.
_ = pricempire
_ = timedelta
