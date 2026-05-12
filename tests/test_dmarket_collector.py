"""DMarket collector tests.

Same matrix as test_skinport_collector.py: parse helpers, happy path,
empty listings, 429 retry, 5xx retry, 4xx no-retry, retry exhaustion,
malformed JSON, request URL shape, plus a guard against the
``suggestedPrice`` trap (must use ``.price.USD``, not the recommendation).

All HTTP mocked via pytest-httpx; ``time.sleep`` monkey-patched out.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from collectors.base import full_jitter_backoff
from collectors.dmarket import (
    DMarketCollector,
    parse_dmarket_price,
)


class TestParseDMarketPrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("4378", Decimal("43.78")),
            ("100", Decimal("1.00")),
            ("1", Decimal("0.01")),
            ("0", Decimal("0.00")),
            (4378, Decimal("43.78")),  # int input also accepted
            ("123456", Decimal("1234.56")),
            (None, None),
            ("", None),
            ("abc", None),
            ("-5", None),  # negative cents nonsensical
        ],
    )
    def test_parse(
        self, raw, expected: Decimal | None
    ) -> None:
        assert parse_dmarket_price(raw) == expected

    def test_preserves_two_decimal_representation(self) -> None:
        """Decimal(100) / Decimal(100) is Decimal('1'); we want
        Decimal('1.00'). Verify the string-slicing path."""
        result = parse_dmarket_price("100")
        assert result is not None
        assert str(result) == "1.00"


class TestFullJitterBackoffReuse:
    """The backoff helper is reused as-is; just sanity-check the bounds
    so a future change to base.py doesn't quietly break this collector."""

    def test_bounds(self) -> None:
        for attempt in range(6):
            upper = min(300.0, 3.0 * (2**attempt))
            for _ in range(50):
                delay = full_jitter_backoff(
                    attempt, base=3.0, cap=300.0
                )
                assert 0.0 <= delay <= upper


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Squash time.sleep inside the collector so retry tests are instant."""
    monkeypatch.setattr("collectors.dmarket.time.sleep", lambda _: None)


def _offers(*prices_usd: str, float_value: float | None = None) -> list[dict]:
    """Build a minimal DMarket-shaped offers array sorted ascending by price."""
    offers = []
    for p in prices_usd:
        offer: dict = {
            "price": {"USD": p},
            # Trap field — collector MUST NOT use this. We put an
            # obviously-wrong value (10x the listing price) so a bug
            # using suggestedPrice would be immediately visible.
            "suggestedPrice": {"USD": str(int(p) * 10)},
        }
        if float_value is not None:
            offer["extra"] = {"floatValue": float_value}
        offers.append(offer)
    return offers


def _body(*prices_usd: str, **kwargs) -> dict:
    return {"objects": _offers(*prices_usd, **kwargs), "total": {}}


class TestDMarketCollectorHTTP:
    def test_happy_path_uses_price_not_suggested(self, httpx_mock) -> None:
        # listing is $43.78; suggestedPrice would be $437.80 if buggy.
        httpx_mock.add_response(json=_body("4378", "5000", "9999"))

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        assert obs is not None
        assert obs.price == Decimal("43.78"), (
            "Got suggestedPrice instead of price.USD"
        )
        assert obs.volume == 3  # number of listings
        assert obs.currency == "USD"
        assert obs.source_name == "dmarket"
        # raw_response carries the cheapest offer + count metadata.
        assert obs.raw_response["cheapest"]["price"]["USD"] == "4378"
        assert obs.raw_response["total_objects_returned"] == 3

    def test_empty_objects_returns_none(self, httpx_mock) -> None:
        # No current listings — DMarket returns objects: [].
        # That's "unavailable", NOT a parse failure.
        httpx_mock.add_response(json={"objects": [], "total": {}})

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "Some Item No One Lists")
        assert obs is None

    def test_float_value_preserved_in_raw_response(self, httpx_mock) -> None:
        """v2 will care about per-listing float; we persist it via
        raw_response.extra.floatValue but do NOT promote it to a
        top-level column."""
        httpx_mock.add_response(
            json=_body("4378", float_value=0.0712)
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )
        assert obs is not None
        assert obs.raw_response["cheapest"]["extra"]["floatValue"] == 0.0712

    def test_429_then_success(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=429)
        httpx_mock.add_response(json=_body("100"))

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")

        assert obs is not None
        assert obs.price == Decimal("1.00")
        assert len(httpx_mock.get_requests()) == 2

    def test_503_then_success(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(json=_body("250"))

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")

        assert obs is not None
        assert obs.price == Decimal("2.50")

    def test_404_no_retry(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=404)

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "Bad Item")

        assert obs is None
        assert len(httpx_mock.get_requests()) == 1

    def test_429_exhausted(self, httpx_mock) -> None:
        for _ in range(DMarketCollector.max_retries):
            httpx_mock.add_response(status_code=429)

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")

        assert obs is None
        assert (
            len(httpx_mock.get_requests()) == DMarketCollector.max_retries
        )

    def test_malformed_json(self, httpx_mock) -> None:
        httpx_mock.add_response(content=b"<html>maintenance</html>")

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")
        assert obs is None

    def test_objects_not_a_list(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json={"objects": "not a list", "total": {}}
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")
        assert obs is None

    def test_cheapest_missing_price_object(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json={"objects": [{"suggestedPrice": {"USD": "1000"}}]}
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")
        assert obs is None

    def test_request_url_carries_all_params(self, httpx_mock) -> None:
        httpx_mock.add_response(json=_body("4378"))

        collector = DMarketCollector()
        with collector.make_client() as client:
            collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        request = httpx_mock.get_requests()[0]
        url = str(request.url)
        assert "gameId=a8db" in url
        assert "currency=USD" in url
        assert "limit=100" in url
        assert "orderBy=price" in url
        assert "orderDir=asc" in url
        assert "AK-47" in url  # title= encoding sanity

    def test_unicode_market_hash_name_in_request(self, httpx_mock) -> None:
        httpx_mock.add_response(json=_body("100000"))
        collector = DMarketCollector()
        name = "★ Karambit | Doppler (Factory New)"
        with collector.make_client() as client:
            collector.collect_one(client, name)
        request = httpx_mock.get_requests()[0]
        # U+2605 ★ encodes as %E2%98%85 in UTF-8 percent-encoding.
        assert "%E2%98%85" in str(request.url)
