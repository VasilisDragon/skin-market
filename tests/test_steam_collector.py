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
    STEAM_OUTLIER_THRESHOLD_PCT,
    SteamCollector,
    _is_steam_outlier,
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


@pytest.fixture(autouse=True)
def _no_outlier_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: ``_seven_day_median`` returns None so the outlier filter
    is inert (None median → no filter, observation passes through).
    Tests that exercise the filter re-monkeypatch this with a concrete
    Decimal median. Existing HTTP-mocked tests are pure: no DB
    connection, no median lookup, original semantics preserved."""
    monkeypatch.setattr(
        "collectors.steam._seven_day_median",
        lambda _name: None,
    )


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

    def test_collect_cycle_aborts_on_first_item_ratelimited(
        self, httpx_mock, monkeypatch
    ) -> None:
        """Integration-shape test: RateLimited raised in ``collect_one``
        must propagate through the default ``collect_cycle`` generator
        AND abort the cycle before any subsequent item is requested.

        Originally caught a fire-drill where the running collector
        container appeared to ignore RateLimited — turned out to be a
        stale Docker image (operator workflow lesson, documented in ADR
        013 §6), but this test now guards the propagation behavior so
        any actual regression here surfaces in pytest.
        """
        # Patch base.time.sleep so the inter_request_delay (5s) doesn't
        # actually pause the test. If the bug were real and item 2 were
        # attempted, this is also where we'd want to know the sleep
        # didn't matter — but the assertion below catches that case
        # anyway.
        monkeypatch.setattr("collectors.base.time.sleep", lambda _: None)
        # Five 429s for item 1; deliberately no response for item 2 —
        # if the cycle incorrectly continues, pytest-httpx will raise
        # "no response found" on the item-2 request.
        for _ in range(SteamCollector.max_retries):
            httpx_mock.add_response(status_code=429)

        collector = SteamCollector()
        with collector.make_client() as client, pytest.raises(
            RateLimited
        ) as excinfo:
            # Consume the generator fully via list() so any yields are
            # collected; if RateLimited propagates correctly, list()
            # raises before getting to item 2.
            list(
                collector.collect_cycle(
                    client, ["item1", "item2", "item3"]
                )
            )

        assert excinfo.value.source_name == "steam_market"
        # Only item 1's 5 retries were issued — items 2 and 3 never
        # reached the HTTP layer.
        assert (
            len(httpx_mock.get_requests()) == SteamCollector.max_retries
        )

    def test_unicode_market_hash_name_in_request(self, httpx_mock) -> None:
        """Make sure ★ and ™ characters reach the wire encoded correctly,
        not mangled by encoding-default surprises."""
        # (ordered before the next test class so the existing test stays here)
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


class TestIsSteamOutlierPureFunction:
    """``_is_steam_outlier(median, observed)`` is a pure decision function;
    test the boundaries without spinning up a collector."""

    def test_none_median_passes(self) -> None:
        # No median → no filter (bias toward data on new items).
        assert _is_steam_outlier(None, Decimal("1.00")) is False
        assert _is_steam_outlier(None, Decimal("0.01")) is False

    def test_well_below_threshold_filtered(self) -> None:
        # Threshold at 20% of 40.00 = 8.00; observation $1.00 is far below.
        assert _is_steam_outlier(Decimal("40.00"), Decimal("1.00")) is True

    def test_strictly_at_threshold_passes(self) -> None:
        # Filter is `<`, not `<=`. Observation exactly at the threshold
        # passes — the threshold is a floor, not a forbidden value.
        threshold_value = Decimal("40.00") * STEAM_OUTLIER_THRESHOLD_PCT
        assert _is_steam_outlier(Decimal("40.00"), threshold_value) is False

    def test_just_below_threshold_filtered(self) -> None:
        threshold_value = Decimal("40.00") * STEAM_OUTLIER_THRESHOLD_PCT
        just_below = threshold_value - Decimal("0.01")
        assert _is_steam_outlier(Decimal("40.00"), just_below) is True

    def test_above_median_passes(self) -> None:
        # Filter only catches LOW outliers; high spikes are real signal.
        assert _is_steam_outlier(Decimal("40.00"), Decimal("100.00")) is False


class TestSteamOutlierFilter:
    """Integration of the outlier filter into ``SteamCollector.collect_one``.

    Median lookup is mocked per-test so the existing DB-free unit-test
    posture is preserved. The pure function (above) covers the
    arithmetic; these tests verify the integration: filter fires →
    DECLINED, filter doesn't fire → PriceObservation, insufficient
    history → PriceObservation."""

    def test_outlier_filtered_when_below_threshold(
        self, monkeypatch, httpx_mock, caplog
    ) -> None:
        # Median $40; observation $1.00 (the live-observed manipulation
        # pattern). 1.00 is 2.5% of median, well below the 20% floor.
        monkeypatch.setattr(
            "collectors.steam._seven_day_median",
            lambda _name: Decimal("40.00"),
        )
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$1.00", "volume": "1"}
        )
        collector = SteamCollector()
        with collector.make_client() as client, caplog.at_level(
            "WARNING", logger="collectors.steam"
        ):
            result = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        assert result is DECLINED, (
            "outlier observation must be rejected (DECLINED), not "
            "returned as a PriceObservation that persist_observation "
            "would later write."
        )
        # Log carries the structured detail operators grep for.
        outlier_logs = [
            r.getMessage()
            for r in caplog.records
            if "Steam outlier filter" in r.getMessage()
        ]
        assert outlier_logs, (
            "expected a 'Steam outlier filter' WARNING log line"
        )
        msg = outlier_logs[-1]
        assert "AK-47" in msg
        assert "median" in msg.lower()
        assert "1.00" in msg or "1" in msg

    def test_normal_observation_passes_filter(
        self, monkeypatch, httpx_mock
    ) -> None:
        # Median $40, observation $35 — within typical market noise,
        # well above the 20% floor. Filter must not fire.
        monkeypatch.setattr(
            "collectors.steam._seven_day_median",
            lambda _name: Decimal("40.00"),
        )
        httpx_mock.add_response(
            json={
                "success": True,
                "lowest_price": "$35.00",
                "volume": "12",
            }
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            result = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        assert result is not DECLINED
        assert result is not None
        assert result.price == Decimal("35.00")

    def test_insufficient_history_passes_filter(
        self, monkeypatch, httpx_mock
    ) -> None:
        """Items with fewer than STEAM_OUTLIER_MIN_OBSERVATIONS rows in
        the 7-day window get None from ``_seven_day_median``; the filter
        is skipped (bias toward letting new-item data land)."""
        monkeypatch.setattr(
            "collectors.steam._seven_day_median",
            lambda _name: None,
        )
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$1.00", "volume": "1"}
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            result = collector.collect_one(
                client, "New Item With No History"
            )

        assert result is not DECLINED
        assert result is not None
        assert result.price == Decimal("1.00")

    def test_threshold_boundary(
        self, monkeypatch, httpx_mock
    ) -> None:
        """Observation EXACTLY at ``median * STEAM_OUTLIER_THRESHOLD_PCT``
        passes (filter is strict less-than, not less-than-or-equal).
        Median $40 × 0.20 = $8.00; an $8.00 observation passes."""
        monkeypatch.setattr(
            "collectors.steam._seven_day_median",
            lambda _name: Decimal("40.00"),
        )
        httpx_mock.add_response(
            json={"success": True, "lowest_price": "$8.00", "volume": "3"}
        )
        collector = SteamCollector()
        with collector.make_client() as client:
            result = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )

        assert result is not DECLINED
        assert result is not None
        assert result.price == Decimal("8.00")
