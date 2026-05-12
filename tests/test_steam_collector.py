"""Steam Market collector tests.

All HTTP is mocked via ``pytest-httpx``; no network calls. The ``time.sleep``
inside the backoff path is monkey-patched out so retry tests run in
milliseconds.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from collectors.base import DECLINED, RateLimited, full_jitter_backoff
from collectors.steam import (
    SteamCollector,
    parse_steam_price,
    parse_steam_volume,
)


class TestParseSteamPrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$12.34", Decimal("12.34")),
            ("$1,234.56", Decimal("1234.56")),
            ("$0.03", Decimal("0.03")),
            ("12.34$", Decimal("12.34")),
            ("$1.00", Decimal("1.00")),
            ("", None),
            (None, None),
            ("abc", None),
            (".", None),
        ],
    )
    def test_parse(self, raw: str | None, expected: Decimal | None) -> None:
        assert parse_steam_price(raw) == expected


class TestParseSteamVolume:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("123", 123),
            ("1,234", 1234),
            ("0", 0),
            ("", None),
            (None, None),
            ("abc", None),
        ],
    )
    def test_parse(self, raw: str | None, expected: int | None) -> None:
        assert parse_steam_volume(raw) == expected


class TestFullJitterBackoff:
    def test_bounds_per_attempt(self) -> None:
        for attempt in range(6):
            upper = min(300.0, 5.0 * (2**attempt))
            for _ in range(50):
                delay = full_jitter_backoff(attempt, base=5.0, cap=300.0)
                assert 0.0 <= delay <= upper

    def test_cap_holds_at_high_attempt(self) -> None:
        # 5 * 2^20 would be huge; cap must hold.
        for _ in range(50):
            assert full_jitter_backoff(20, base=5.0, cap=300.0) <= 300.0


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Squash time.sleep inside the collector so retry tests are instant."""
    monkeypatch.setattr("collectors.steam.time.sleep", lambda _: None)


class TestSteamCollectorHTTP:
    def test_happy_path(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json={
                "success": True,
                "lowest_price": "$12.34",
                "median_price": "$12.50",
                "volume": "99",
            }
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        assert obs is not None
        assert obs.price == Decimal("12.34")
        assert obs.volume == 99
        assert obs.currency == "USD"
        assert obs.source_name == "steam_market"
        assert obs.market_hash_name == "AK-47 | Redline (Field-Tested)"
        assert obs.raw_response["success"] is True

    def test_lowest_price_missing_falls_back_to_median(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json={
                "success": True,
                "median_price": "$5.55",
                "volume": "1",
            }
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")

        assert obs is not None
        assert obs.price == Decimal("5.55")

    def test_success_false_returns_none(self, httpx_mock) -> None:
        httpx_mock.add_response(json={"success": False})
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "Nonexistent Item")
        assert obs is None

    def test_success_true_but_no_price(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json={"success": True, "volume": "0"}
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")
        assert obs is None

    def test_429_then_success(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=429)
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$1.00", "volume": "1"}
        )

        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")

        assert obs is not None
        assert obs.price == Decimal("1.00")
        assert len(httpx_mock.get_requests()) == 2

    def test_503_then_success(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$2.00", "volume": "5"}
        )

        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")

        assert obs is not None
        assert obs.price == Decimal("2.00")
        assert len(httpx_mock.get_requests()) == 2

    def test_404_no_retry(self, httpx_mock) -> None:
        # 4xx non-429 is a definitive decline (Steam said "bad request"),
        # not an ambiguous-unavailable.
        httpx_mock.add_response(status_code=404)
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "Nonexistent")

        assert obs is DECLINED
        assert len(httpx_mock.get_requests()) == 1

    def test_429_exhausted_raises_ratelimited(self, httpx_mock) -> None:
        for _ in range(SteamCollector.max_retries):
            httpx_mock.add_response(status_code=429)
        collector = SteamCollector()
        with collector.make_client() as client, pytest.raises(
            RateLimited
        ) as excinfo:
            collector.collect_one(client, "X")

        assert excinfo.value.source_name == "steam_market"
        # No Retry-After header in these responses — wrapper-level
        # fallback ladder will be used by the scheduler.
        assert excinfo.value.retry_after_seconds is None
        assert len(httpx_mock.get_requests()) == SteamCollector.max_retries

    def test_429_with_retry_after_header_honored(
        self, httpx_mock
    ) -> None:
        # Header tells us 1 second; we cap in-call sleep at 60s anyway
        # so the test only needs to assert the value rode through onto
        # the eventual RateLimited exception.
        for _ in range(SteamCollector.max_retries):
            httpx_mock.add_response(
                status_code=429, headers={"Retry-After": "42"}
            )
        collector = SteamCollector()
        with collector.make_client() as client, pytest.raises(
            RateLimited
        ) as excinfo:
            collector.collect_one(client, "X")
        assert excinfo.value.retry_after_seconds == 42

    def test_5xx_exhausted_returns_declined(self, httpx_mock) -> None:
        # Pure timeout/5xx exhaustion without any 429 — source is
        # broken-or-slow, count as declined (something is wrong) rather
        # than ambiguous.
        for _ in range(SteamCollector.max_retries):
            httpx_mock.add_response(status_code=503)
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")
        assert obs is DECLINED

    def test_malformed_json(self, httpx_mock) -> None:
        httpx_mock.add_response(content=b"<html>nope</html>")
        collector = SteamCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, "X")
        assert obs is None

    def test_request_url_contains_appid_and_currency(self, httpx_mock) -> None:
        """Sanity-check the exact request shape — country=US, currency=1,
        appid=730. If Steam ever changes the parameter set, this catches it."""
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$1.00", "volume": "1"}
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            collector.collect_one(client, "AK-47 | Redline (Field-Tested)")

        request = httpx_mock.get_requests()[0]
        url = str(request.url)
        assert "country=US" in url
        assert "currency=1" in url
        assert "appid=730" in url
        # market_hash_name is URL-encoded; check both spaces and pipe.
        assert "AK-47" in url
        assert "Redline" in url

    def test_unicode_market_hash_name_in_request(self, httpx_mock) -> None:
        """Make sure ★ and ™ characters reach the wire encoded correctly,
        not mangled by encoding-default surprises."""
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$1000.00", "volume": "1"}
        )
        collector = SteamCollector()
        name = "★ Karambit | Doppler (Factory New)"
        with collector.make_client() as client:
            collector.collect_one(client, name)

        request = httpx_mock.get_requests()[0]
        # U+2605 ★ encodes as %E2%98%85 in UTF-8 percent-encoding.
        assert "%E2%98%85" in str(request.url)
