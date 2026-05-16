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
from db.models import Item, PricempireItemMetadata, PricempireObservation


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
                text(
                    "DELETE FROM pricempire_item_metadata "
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
            text(
                "DELETE FROM pricempire_item_metadata "
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
    collector reads. ``liquidity`` is intentionally an irrational-style
    float (62.802508437142585) and ``rank`` is a numeric string,
    mirroring the real response shape. These shapes used to silently
    fail when stored as JSONB because ijson decoded them as Decimal
    and psycopg's JSON encoder rejected Decimal — fixed by passing
    ``use_float=True`` to ijson. Tests now keep this path live."""
    return {
        "market_hash_name": name,
        "liquidity": 62.802508437142585,
        "rank": "100",
        "marketcap": "215473980",
        "prices": prices or [],
    }


def _wire_price_row(
    *,
    provider_key: str,
    price_cents: int,
    count: int | None = 10,
    updated_at: str = "2026-05-15T20:00:00.000Z",
    last_checked_at: str = "2026-05-15T20:30:00.000Z",
    include_meta: bool = False,
) -> dict:
    """Build one entry of an item's nested ``prices`` array.

    ``include_meta`` adds a realistic ``meta`` dict carrying a float
    exchange ``rate`` — that field tripped a Decimal-not-JSON-
    serializable error in the first live cycle. Keep the path
    exercised in tests.
    """
    row = {
        "price": price_cents,
        "count": count,
        "updated_at": updated_at,
        "last_checked_at": last_checked_at,
        "provider_key": provider_key,
        "meta": None,
        "original_price": None,
        "exchange_rate": None,
    }
    if include_meta:
        row["meta"] = {
            "rate": 0.1468709151526723,
            "original_price": 117900,
            "original_currency": "CNY",
        }
    return row


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

    def test_swapgg_to_underscore_form(self) -> None:
        """Pricempire's wire key is 'swapgg' (no dot, no underscore —
        empirically verified after a 400 with the alternative); our
        source name uses 'pricempire_swap_gg' for Postgres-friendly
        readability. The mapping is the single place this normalization
        happens — guard against regressions."""
        assert (
            _PROVIDER_KEY_TO_SOURCE_NAME["swapgg"]
            == "pricempire_swap_gg"
        )
        assert "swap.gg" not in _PROVIDER_KEY_TO_SOURCE_NAME, (
            "swap.gg is NOT Pricempire's wire key — see "
            "_PROVIDER_KEY_TO_SOURCE_NAME for the verified form."
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
                f"%2Cdmarket%2Cskinport%2Cswapgg"
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
                    f"%2Cdmarket%2Cskinport%2Cswapgg"
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
                    f"%2Cdmarket%2Cskinport%2Cswapgg"
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
                f"%2Cdmarket%2Cskinport%2Cswapgg"
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
                f"%2Cdmarket%2Cskinport%2Cswapgg"
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
                f"%2Cdmarket%2Cskinport%2Cswapgg"
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

    def test_jsonb_safe_for_float_fields(
        self, httpx_mock, sentinel_item
    ) -> None:
        """Regression: ijson's default decoder maps JSON floats to
        Decimal, which psycopg's JSON encoder rejects. The first live
        cycle blew up on liquidity/meta.rate floats. Pinned via
        ``use_float=True`` to ijson + a wire_row carrying those float
        fields here.
        """
        body = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="buff163",
                            price_cents=17316,
                            count=593,
                            include_meta=True,  # meta.rate is a float
                        )
                    ],
                )
            ]
        )
        httpx_mock.add_response(
            url=(
                f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
                f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
                f"%2Cdmarket%2Cskinport%2Cswapgg"
            ),
            content=body,
            headers={"Content-Type": "application/json"},
        )
        collect_snapshot()  # must not raise

        # Confirm the row landed and raw_response carries the floats.
        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(
                    PricempireObservation.price,
                    PricempireObservation.raw_response,
                ).where(PricempireObservation.item_id == sentinel_item)
            ).first()
        assert row is not None
        assert row.price == Decimal("173.16")
        # meta.rate round-trips through JSONB as a float.
        assert row.raw_response["meta"]["rate"] == pytest.approx(
            0.1468709151526723
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
                f"%2Cdmarket%2Cskinport%2Cswapgg"
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

    def test_cycle_log_per_provider_breakdown(
        self, httpx_mock, sentinel_item, caplog
    ) -> None:
        """Cycle 1 writes one row per (resolved) provider; cycle 2
        re-issues the same prices and everything dedup's. The
        cycle-complete log line must surface BOTH the aggregate counts
        AND the per-provider breakdown in a stable alphabetical order
        with zero-row providers explicit, so operators can spot
        provider-specific quiet periods (e.g. swap_gg per §6 of
        docs/phase2a-ingest-validation.md).
        """
        # Cycle 1: one row for two distinct providers. The other four
        # providers contribute zero writes — they MUST still appear in
        # the breakdown as `=0` so the line is shape-stable.
        body_c1 = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="buff163",
                            price_cents=17316,
                            count=593,
                        ),
                        _wire_price_row(
                            provider_key="skinport",
                            price_cents=2800,
                            count=27,
                        ),
                    ],
                ),
            ]
        )
        # Cycle 2: identical body so both rows dedup. The per-provider
        # unchanged breakdown should show buff163=1, skinport=1 and
        # zeros for the other four.
        body_c2 = _make_response(
            [
                _wire_item(
                    _SENTINEL_NAME,
                    prices=[
                        _wire_price_row(
                            provider_key="buff163",
                            price_cents=17316,
                            count=593,
                        ),
                        _wire_price_row(
                            provider_key="skinport",
                            price_cents=2800,
                            count=27,
                        ),
                    ],
                ),
            ]
        )
        url = (
            f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
            f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
            f"%2Cdmarket%2Cskinport%2Cswapgg"
        )
        httpx_mock.add_response(
            url=url,
            content=body_c1,
            headers={"Content-Type": "application/json"},
        )
        httpx_mock.add_response(
            url=url,
            content=body_c2,
            headers={"Content-Type": "application/json"},
        )

        with caplog.at_level("INFO", logger="collectors.pricempire"):
            collect_snapshot()
            collect_snapshot()

        cycle_messages = [
            r.getMessage()
            for r in caplog.records
            if "cycle complete" in r.getMessage()
        ]
        assert len(cycle_messages) == 2, (
            f"expected 2 cycle-complete lines, got {len(cycle_messages)}"
        )
        c1, c2 = cycle_messages

        # Aggregate counts preserved.
        assert "2 rows written" in c1
        assert "0 unchanged" in c1
        assert "0 rows written" in c2
        assert "2 unchanged" in c2

        # Per-provider breakdown present in both cycles.
        # Cycle 1: rows-written breakdown has buff163=1 and skinport=1.
        expected_c1_written = (
            "rows written (buff163=1, buff163_buy=0, csmoney=0, "
            "dmarket=0, skinport=1, swap_gg=0)"
        )
        assert expected_c1_written in c1, (
            f"cycle 1 missing per-provider written breakdown\n"
            f"got: {c1}"
        )
        # Cycle 1 unchanged breakdown: all zeros (everything was written).
        expected_c1_unchanged = (
            "unchanged (buff163=0, buff163_buy=0, csmoney=0, "
            "dmarket=0, skinport=0, swap_gg=0)"
        )
        assert expected_c1_unchanged in c1, (
            f"cycle 1 missing per-provider unchanged breakdown\n"
            f"got: {c1}"
        )

        # Cycle 2: rows-written all zeros, unchanged has the two
        # providers we sent.
        expected_c2_written = (
            "rows written (buff163=0, buff163_buy=0, csmoney=0, "
            "dmarket=0, skinport=0, swap_gg=0)"
        )
        expected_c2_unchanged = (
            "unchanged (buff163=1, buff163_buy=0, csmoney=0, "
            "dmarket=0, skinport=1, swap_gg=0)"
        )
        assert expected_c2_written in c2, (
            f"cycle 2 missing per-provider written breakdown\n"
            f"got: {c2}"
        )
        assert expected_c2_unchanged in c2, (
            f"cycle 2 missing per-provider unchanged breakdown\n"
            f"got: {c2}"
        )


# ────────────────────────────────────────────────────────────────────
# Item-metadata extraction (Phase 2a follow-up, ADR 020)
# ────────────────────────────────────────────────────────────────────


def _wire_item_full_metadata(
    name: str,
    *,
    rank: object = "554",
    liquidity: object = 62.802508437142585,
    marketcap: object = "215473980",
    count: object = "9060",
    trades_7d: object = "29",
    trades_30d: object = "42",
    trades_90d: object = "29",
    steam_last_7d: object = "64",
    steam_last_30d: object = "757",
    steam_last_90d: object = "1691",
    prices: list[dict] | None = None,
) -> dict:
    """A wire item with the full metadata field set the collector
    extracts. Defaults mirror the Glock-18 | Gamma Doppler (FN)
    sample in ``docs/pre-phase2-pricempire-samples/filtered-sources.json``
    — all integer-valued metadata as numeric strings, ``liquidity`` as
    a native float. Override individual fields to exercise the
    defensive parser. ``steam_last_24h`` is intentionally absent (per
    ADR 020: ``/prices`` doesn't carry it; only ``/metas`` does).
    """
    return {
        "market_hash_name": name,
        "rank": rank,
        "liquidity": liquidity,
        "marketcap": marketcap,
        "count": count,
        "trades_7d": trades_7d,
        "trades_30d": trades_30d,
        "trades_90d": trades_90d,
        "steam_last_7d": steam_last_7d,
        "steam_last_30d": steam_last_30d,
        "steam_last_90d": steam_last_90d,
        "prices": prices
        or [
            _wire_price_row(provider_key="buff163", price_cents=17316)
        ],
    }


@_db_required
class TestMetadataExtraction:
    """Per-item metadata writes to pricempire_item_metadata (ADR 020).

    Five test cases mirroring the structure of TestCollectSnapshot:

    1. Full extraction from a realistic wire item.
    2. Dedup gate suppresses an identical second cycle.
    3. Null fields stay null (defensive parsing handles missing).
    4. Numeric strings + native numbers are both coerced cleanly.
    5. A changed field cycles writes a new row.
    """

    _MOCK_URL = (
        f"{PRICEMPIRE_BASE_URL}{PRICEMPIRE_PRICES_PATH}"
        f"?app_id=730&sources=buff163%2Cbuff163_buy%2Ccsmoney"
        f"%2Cdmarket%2Cskinport%2Cswapgg"
    )

    def _mock(self, httpx_mock, items: list[dict]) -> None:
        httpx_mock.add_response(
            url=self._MOCK_URL,
            content=_make_response(items),
            headers={"Content-Type": "application/json"},
        )

    def test_full_extraction_from_realistic_wire_item(
        self, httpx_mock, sentinel_item
    ) -> None:
        self._mock(
            httpx_mock,
            [_wire_item_full_metadata(_SENTINEL_NAME)],
        )
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(
                    PricempireItemMetadata.rank,
                    PricempireItemMetadata.liquidity,
                    PricempireItemMetadata.marketcap,
                    PricempireItemMetadata.count,
                    PricempireItemMetadata.trades_7d,
                    PricempireItemMetadata.trades_30d,
                    PricempireItemMetadata.trades_90d,
                    PricempireItemMetadata.steam_last_24h,
                    PricempireItemMetadata.steam_last_7d,
                    PricempireItemMetadata.steam_last_30d,
                    PricempireItemMetadata.steam_last_90d,
                ).where(
                    PricempireItemMetadata.item_id == sentinel_item
                )
            ).first()
        assert row is not None
        # Numeric-string fields all become typed ints.
        assert row.rank == 554
        assert row.marketcap == 215473980
        assert row.count == 9060
        assert row.trades_7d == 29
        assert row.trades_30d == 42
        assert row.trades_90d == 29
        assert row.steam_last_7d == 64
        assert row.steam_last_30d == 757
        assert row.steam_last_90d == 1691
        # Native float quantizes to NUMERIC(6,2).
        assert row.liquidity == Decimal("62.80")
        # /prices doesn't carry steam_last_24h — always NULL today.
        assert row.steam_last_24h is None

    def test_dedup_gate_skips_identical_cycle(
        self, httpx_mock, sentinel_item
    ) -> None:
        """Two cycles with identical metadata yield one row, not two.
        The dedup tuple comparison is the load-bearing piece — without
        it, every cycle would write 48 rows for nothing."""
        for _ in range(2):
            self._mock(
                httpx_mock,
                [_wire_item_full_metadata(_SENTINEL_NAME)],
            )
        collect_snapshot()
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            rows = session.execute(
                select(PricempireItemMetadata).where(
                    PricempireItemMetadata.item_id == sentinel_item
                )
            ).all()
        assert len(rows) == 1, (
            f"dedup should keep this at 1 row; got {len(rows)}"
        )

    def test_null_fields_persist_as_null(
        self, httpx_mock, sentinel_item
    ) -> None:
        """Pricempire returns null for several fields on low-liquidity
        items (e.g. ``trades_*`` are commonly null for Souvenir
        Dragon Lores). The parser must accept None without crashing
        and persist NULL — not zero, not Decimal('0')."""
        self._mock(
            httpx_mock,
            [
                _wire_item_full_metadata(
                    _SENTINEL_NAME,
                    rank=None,
                    liquidity=None,
                    marketcap=None,
                    trades_7d=None,
                    trades_30d=None,
                    trades_90d=None,
                )
            ],
        )
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(
                    PricempireItemMetadata.rank,
                    PricempireItemMetadata.liquidity,
                    PricempireItemMetadata.marketcap,
                    PricempireItemMetadata.trades_7d,
                    PricempireItemMetadata.trades_30d,
                    PricempireItemMetadata.trades_90d,
                    PricempireItemMetadata.count,  # NOT nulled; sanity
                ).where(
                    PricempireItemMetadata.item_id == sentinel_item
                )
            ).first()
        assert row is not None
        assert row.rank is None
        assert row.liquidity is None
        assert row.marketcap is None
        assert row.trades_7d is None
        assert row.trades_30d is None
        assert row.trades_90d is None
        # Non-nulled control: count stays populated.
        assert row.count == 9060

    def test_numeric_string_and_native_number_both_coerced(
        self, httpx_mock, sentinel_item
    ) -> None:
        """Pricempire's wire types are inconsistent across endpoints:
        /prices delivers integers as numeric strings, /metas as native
        numbers. The collector reads /prices today, but the parser is
        tolerant of both forms for forward-compat with a metas-cron."""
        self._mock(
            httpx_mock,
            [
                _wire_item_full_metadata(
                    _SENTINEL_NAME,
                    rank="554",          # numeric string (/prices form)
                    marketcap=215473980, # native int (/metas form)
                    trades_7d=29,        # native int (/metas form)
                    count="9060",        # numeric string (/prices form)
                    liquidity=75,        # native int as liquidity (/metas form)
                )
            ],
        )
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            row = session.execute(
                select(
                    PricempireItemMetadata.rank,
                    PricempireItemMetadata.marketcap,
                    PricempireItemMetadata.trades_7d,
                    PricempireItemMetadata.count,
                    PricempireItemMetadata.liquidity,
                ).where(
                    PricempireItemMetadata.item_id == sentinel_item
                )
            ).first()
        assert row is not None
        assert row.rank == 554
        assert row.marketcap == 215473980
        assert row.trades_7d == 29
        assert row.count == 9060
        # Liquidity 75 (int) → Decimal('75.00') after quantize.
        assert row.liquidity == Decimal("75.00")

    def test_changed_field_writes_new_row(
        self, httpx_mock, sentinel_item
    ) -> None:
        """A second cycle where any single field differs writes a
        second row — the dedup gate must catch only *identical*
        tuples, not approximately-equal ones."""
        self._mock(
            httpx_mock,
            [_wire_item_full_metadata(_SENTINEL_NAME, rank="554")],
        )
        collect_snapshot()
        # Cycle 2: rank shifts one position. Everything else same.
        self._mock(
            httpx_mock,
            [_wire_item_full_metadata(_SENTINEL_NAME, rank="555")],
        )
        collect_snapshot()

        engine = get_engine()
        with Session(engine) as session:
            rows = session.execute(
                select(
                    PricempireItemMetadata.rank
                ).where(
                    PricempireItemMetadata.item_id == sentinel_item
                ).order_by(PricempireItemMetadata.timestamp)
            ).all()
        ranks = [r.rank for r in rows]
        assert ranks == [554, 555]


# Reference unused import to keep ruff happy if a later edit removes
# the only consumer.
_ = pricempire
_ = timedelta
