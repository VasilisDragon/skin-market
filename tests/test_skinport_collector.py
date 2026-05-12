"""Skinport collector tests. All HTTP mocked via pytest-httpx."""

from __future__ import annotations

from decimal import Decimal

import pytest

from collectors.skinport import (
    SkinportCollector,
    parse_skinport_price,
)


class TestParseSkinportPrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (42.5, Decimal("42.5")),
            (42, Decimal("42")),
            ("42.50", Decimal("42.50")),
            (0, Decimal("0")),
            (0.01, Decimal("0.01")),
            (None, None),
            ("", None),
            ("abc", None),
        ],
    )
    def test_parse(
        self, raw: float | int | str | None, expected: Decimal | None
    ) -> None:
        assert parse_skinport_price(raw) == expected

    def test_no_float_imprecision(self) -> None:
        # Decimal(float) injects ulps; we route through str() to avoid it.
        # 42.50 -> str() -> "42.5" -> Decimal("42.5") is correct.
        result = parse_skinport_price(42.50)
        assert result is not None
        # No "42.50000000000..." in the output.
        assert str(result) == "42.5"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("collectors.skinport.time.sleep", lambda _: None)


def _bulk_response_with(*items: dict) -> list[dict]:
    """Build a minimal Skinport-shaped response from per-item dicts.

    Fills in fields a real response carries that we don't currently read
    but might in future tests (max_price etc.), so this matches what a
    realistic mock looks like.
    """
    out = []
    for it in items:
        out.append(
            {
                "currency": "USD",
                "max_price": None,
                "mean_price": None,
                "median_price": None,
                **it,
            }
        )
    return out


class TestSkinportCollectorCycle:
    def test_happy_path_single_match(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "min_price": 42.50,
                    "quantity": 27,
                },
                {
                    "market_hash_name": "M4A4 | Howl (Factory New)",
                    "min_price": 1500.00,
                    "quantity": 3,
                },
                {
                    "market_hash_name": "Some Item Not On Our Watchlist",
                    "min_price": 9.99,
                    "quantity": 100,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client,
                    [
                        "AK-47 | Redline (Field-Tested)",
                        "M4A4 | Howl (Factory New)",
                    ],
                )
            )

        assert len(results) == 2
        assert all(r is not None for r in results)
        assert results[0].price == Decimal("42.5")
        assert results[0].volume == 27
        assert results[0].currency == "USD"
        assert results[0].source_name == "skinport"
        assert results[1].price == Decimal("1500.00")
        assert results[1].volume == 3

    def test_yields_none_for_unlisted_watchlist_item(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "min_price": 42.50,
                    "quantity": 27,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client,
                    [
                        "AK-47 | Redline (Field-Tested)",
                        "Item Skinport Does Not Sell",
                    ],
                )
            )

        assert len(results) == 2
        assert results[0] is not None
        assert results[1] is None

    def test_yields_none_when_min_price_null(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "min_price": None,  # no listings
                    "quantity": 0,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client, ["AK-47 | Redline (Field-Tested)"]
                )
            )

        assert results == [None]

    def test_429_then_success(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=429)
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "min_price": 1.00,
                    "quantity": 1,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client, ["AK-47 | Redline (Field-Tested)"]
                )
            )

        assert len(results) == 1
        assert results[0] is not None
        assert results[0].price == Decimal("1.00")
        assert len(httpx_mock.get_requests()) == 2

    def test_5xx_then_success(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "min_price": 2.00,
                    "quantity": 1,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client, ["AK-47 | Redline (Field-Tested)"]
                )
            )

        assert len(results) == 1
        assert results[0].price == Decimal("2.00")

    def test_404_no_retry_yields_nones(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=404)

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client,
                    [
                        "AK-47 | Redline (Field-Tested)",
                        "M4A4 | Howl (Factory New)",
                    ],
                )
            )

        assert results == [None, None]
        assert len(httpx_mock.get_requests()) == 1

    def test_retry_exhaustion(self, httpx_mock) -> None:
        for _ in range(SkinportCollector.max_retries):
            httpx_mock.add_response(status_code=429)

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client, ["AK-47 | Redline (Field-Tested)"]
                )
            )

        assert results == [None]
        assert (
            len(httpx_mock.get_requests()) == SkinportCollector.max_retries
        )

    def test_non_list_body_yields_nones(self, httpx_mock) -> None:
        httpx_mock.add_response(json={"oops": "we changed the shape"})

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client,
                    [
                        "AK-47 | Redline (Field-Tested)",
                        "M4A4 | Howl (Factory New)",
                    ],
                )
            )

        assert results == [None, None]

    def test_malformed_json_yields_nones(self, httpx_mock) -> None:
        httpx_mock.add_response(content=b"<html>maintenance</html>")

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client, ["AK-47 | Redline (Field-Tested)"]
                )
            )

        assert results == [None]

    def test_request_url_carries_app_id_and_currency(
        self, httpx_mock
    ) -> None:
        httpx_mock.add_response(json=[])
        collector = SkinportCollector()
        with collector.make_client() as client:
            list(
                collector.collect_cycle(
                    client, ["AK-47 | Redline (Field-Tested)"]
                )
            )

        request = httpx_mock.get_requests()[0]
        url = str(request.url)
        assert "app_id=730" in url
        assert "currency=USD" in url

    def test_collect_one_wraps_cycle(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "min_price": 42.50,
                    "quantity": 27,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        assert obs is not None
        assert obs.price == Decimal("42.5")

    def test_nfc_normalized_lookup(self, httpx_mock) -> None:
        """Watchlist name and Skinport response both NFC-normalize the
        same way, so the ™/★ codepoints round-trip through the lookup."""
        httpx_mock.add_response(
            json=_bulk_response_with(
                {
                    "market_hash_name": (
                        "StatTrak™ AK-47 | Redline (Field-Tested)"
                    ),
                    "min_price": 99.99,
                    "quantity": 5,
                },
            )
        )

        collector = SkinportCollector()
        with collector.make_client() as client:
            results = list(
                collector.collect_cycle(
                    client,
                    ["StatTrak™ AK-47 | Redline (Field-Tested)"],
                )
            )

        assert len(results) == 1
        assert results[0] is not None
        assert results[0].price == Decimal("99.99")
