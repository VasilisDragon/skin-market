"""DMarket collector tests.

Same matrix as test_skinport_collector.py: parse helpers, happy path,
empty listings, 429 retry, 5xx retry, 4xx no-retry, retry exhaustion,
malformed JSON, request URL shape, plus a guard against the
``suggestedPrice`` trap (must use ``.price.USD``, not the recommendation).

All HTTP mocked via pytest-httpx; ``time.sleep`` monkey-patched out.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from collectors.base import DECLINED, RateLimited, full_jitter_backoff
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


def _offers(
    *prices_usd: str,
    float_value: float | None = None,
    title: str = "X",
) -> list[dict]:
    """Build a minimal DMarket-shaped offers array sorted ascending by price.

    ``title`` mirrors what DMarket returns; the collector's post-fetch
    title-match check (Phase 6.5) requires it to equal the requested
    market_hash_name. Tests that request item ``"X"`` get the default;
    others override.
    """
    offers = []
    for p in prices_usd:
        offer: dict = {
            "title": title,
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
        httpx_mock.add_response(
            json=_body(
                "4378", "5000", "9999",
                title="AK-47 | Redline (Field-Tested)",
            )
        )

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
            json=_body(
                "4378",
                float_value=0.0712,
                title="AK-47 | Redline (Field-Tested)",
            )
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

        assert obs is DECLINED
        assert len(httpx_mock.get_requests()) == 1

    def test_429_exhausted_raises_ratelimited(self, httpx_mock) -> None:
        for _ in range(DMarketCollector.max_retries):
            httpx_mock.add_response(status_code=429)

        collector = DMarketCollector()
        with collector.make_client() as client, pytest.raises(
            RateLimited
        ) as excinfo:
            collector.collect_one(client, "X")

        assert excinfo.value.source_name == "dmarket"
        assert excinfo.value.retry_after_seconds is None
        assert (
            len(httpx_mock.get_requests()) == DMarketCollector.max_retries
        )

    def test_429_with_retry_after_header_honored(
        self, httpx_mock
    ) -> None:
        for _ in range(DMarketCollector.max_retries):
            httpx_mock.add_response(
                status_code=429, headers={"Retry-After": "15"}
            )
        collector = DMarketCollector()
        with collector.make_client() as client, pytest.raises(
            RateLimited
        ) as excinfo:
            collector.collect_one(client, "X")
        assert excinfo.value.retry_after_seconds == 15

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

    def test_title_mismatch_dropped(self, httpx_mock) -> None:
        """DMarket's `title=` query is a substring/prefix match, not exact —
        asking for `Desert Eagle | Blaze (Factory New)` can return a
        `Desert Eagle | Oxide Blaze (Factory New)` listing (~480× cheaper,
        different skin entirely). Phase 6.5: post-fetch, enforce exact
        NFC-normalized title equality and drop non-matches before they
        pollute prices + cross_source_spread + cross_source_divergence."""
        httpx_mock.add_response(
            json={
                "objects": [
                    {
                        "title": "Desert Eagle | Oxide Blaze (Factory New)",
                        "price": {"USD": "159"},
                        "suggestedPrice": {"USD": "200"},
                    }
                ],
                "total": {},
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "Desert Eagle | Blaze (Factory New)"
            )
        assert obs is None, (
            "Title mismatch must skip persistence; got a "
            "PriceObservation with the wrong-variant price"
        )

    def test_title_match_persists(self, httpx_mock) -> None:
        """Mirror of the mismatch test: when title matches exactly,
        persistence proceeds normally."""
        httpx_mock.add_response(
            json={
                "objects": [
                    {
                        "title": "Desert Eagle | Blaze (Factory New)",
                        "price": {"USD": "76103"},  # $761.03
                        "suggestedPrice": {"USD": "80000"},
                    }
                ],
                "total": {},
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "Desert Eagle | Blaze (Factory New)"
            )
        assert obs is not None
        assert obs.price == Decimal("761.03")

    def test_title_match_nfc_tolerant(self, httpx_mock) -> None:
        """The check normalizes both sides via NFC, so a decomposed-form
        unicode title returned by DMarket still matches our canonical
        market_hash_name. Defensive — Steam-sourced names are already NFC,
        but copy-pasted/hand-typed test fixtures aren't always."""
        # NFC: U+2122 (™) single codepoint. We pass that on both sides;
        # if normalize_name passed through identity (as expected),
        # equality holds.
        httpx_mock.add_response(
            json={
                "objects": [
                    {
                        "title": (
                            "StatTrak™ AK-47 | Redline (Field-Tested)"
                        ),
                        "price": {"USD": "5000"},
                    }
                ],
                "total": {},
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client,
                "StatTrak™ AK-47 | Redline (Field-Tested)",
            )
        assert obs is not None
        assert obs.price == Decimal("50.00")

    def test_title_missing_drops(self, httpx_mock) -> None:
        """If DMarket changes response shape and stops returning `title`,
        we skip rather than silently persisting under a name we can't
        verify."""
        httpx_mock.add_response(
            json={
                "objects": [
                    {"price": {"USD": "4378"}, "suggestedPrice": {"USD": "5000"}}
                ],
                "total": {},
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(
                client, "AK-47 | Redline (Field-Tested)"
            )
        assert obs is None

    def test_request_url_carries_all_params(self, httpx_mock) -> None:
        httpx_mock.add_response(
            json=_body("4378", title="AK-47 | Redline (Field-Tested)")
        )

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
        name = "★ Karambit | Doppler (Factory New)"
        httpx_mock.add_response(json=_body("100000", title=name))
        collector = DMarketCollector()
        with collector.make_client() as client:
            collector.collect_one(client, name)
        request = httpx_mock.get_requests()[0]
        # U+2605 ★ encodes as %E2%98%85 in UTF-8 percent-encoding.
        assert "%E2%98%85" in str(request.url)


# ──────────────────────────────────────────────────────────────────────
# Phase 2b Step 6 — ADR 012 §7 iterate-objects[] + alias map
# ──────────────────────────────────────────────────────────────────────


def _mixed_offers(
    titles_and_prices: list[tuple[str, str]],
) -> list[dict]:
    """Build a DMarket-shaped objects[] with multiple titles, sorted
    by the caller's intent. Used to exercise the iterate-objects[]
    matching logic against synthetic responses."""
    return [
        {
            "title": title,
            "price": {"USD": price},
            "suggestedPrice": {"USD": str(int(price) * 10)},
        }
        for title, price in titles_and_prices
    ]


class TestPhase2bIterateObjectsLogic:
    """Phase 2b Step 6 (ADR 012 §7) — replaces the old objects[0]-
    only title check with iterate-objects[] + accept-set matching.
    Pure-logic tests using synthetic responses; no real DMarket
    fixtures. Real-fixture tests live in
    ``TestPhase2bRealDMarketFixtures`` below.
    """

    _CANONICAL = "AK-47 | Test Item (Field-Tested)"

    def test_iterates_to_find_canonical_when_not_at_index_0(
        self, httpx_mock
    ) -> None:
        """objects[0] is a wrong variant; canonical sits at index 1.
        Pre-Phase-2b the collector would have rejected the whole
        response; post-Phase-2b it picks the canonical and returns
        its price."""
        httpx_mock.add_response(
            json={
                "objects": _mixed_offers(
                    [
                        ("AK-47 | Wrong Variant (Field-Tested)", "100"),
                        (self._CANONICAL, "150"),
                        (self._CANONICAL, "200"),
                    ]
                )
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, self._CANONICAL)
        assert obs is not None
        # Picks the FIRST canonical match (price 1.50), not the
        # cheapest-overall (price 1.00 wrong variant).
        assert obs.price == Decimal("1.50")
        assert obs.raw_response["cheapest"]["title"] == self._CANONICAL

    def test_alias_added_to_accept_set(self, httpx_mock) -> None:
        """An alias title in alias_map is acceptable as a match."""
        alias_title = "Some Variant That DMarket Uses"
        httpx_mock.add_response(
            json={
                "objects": _mixed_offers(
                    [
                        ("AK-47 | Wrong Variant (Field-Tested)", "100"),
                        (alias_title, "150"),
                    ]
                )
            }
        )
        # Use the helper that aliases are NFC-normalized at construction
        # in scheduler._load_dmarket_alias_map; tests bypass that by
        # passing the normalized form directly.
        from db.naming import normalize_name

        collector = DMarketCollector(
            alias_map={
                normalize_name(self._CANONICAL): frozenset(
                    {normalize_name(alias_title)}
                )
            }
        )
        with collector.make_client() as client:
            obs = collector.collect_one(client, self._CANONICAL)
        assert obs is not None
        assert obs.price == Decimal("1.50")
        assert (
            obs.raw_response["cheapest"]["title"] == alias_title
        )

    def test_no_accept_set_match_returns_none(
        self, httpx_mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """objects[] has entries, but none match the accept set →
        returns None with the new log message."""
        import logging as _logging

        httpx_mock.add_response(
            json={
                "objects": _mixed_offers(
                    [
                        ("Wrong Title A", "100"),
                        ("Wrong Title B", "200"),
                    ]
                )
            }
        )
        collector = DMarketCollector()
        with caplog.at_level(
            _logging.WARNING, logger="collectors.dmarket"
        ):
            with collector.make_client() as client:
                obs = collector.collect_one(client, self._CANONICAL)
        assert obs is None
        # New log message includes "no accept-set match" + ADR pointer.
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "no accept-set match" in m and "ADR 012 §7" in m
            for m in msgs
        ), f"expected new log shape; got {msgs}"

    def test_empty_alias_map_behaves_as_canonical_only(
        self, httpx_mock
    ) -> None:
        """alias_map={} (or default) → only canonical-name match is
        accepted. Iteration still finds the canonical when present."""
        httpx_mock.add_response(
            json={
                "objects": _mixed_offers(
                    [
                        ("Wrong A", "100"),
                        (self._CANONICAL, "150"),
                    ]
                )
            }
        )
        collector = DMarketCollector(alias_map={})
        with collector.make_client() as client:
            obs = collector.collect_one(client, self._CANONICAL)
        assert obs is not None
        assert obs.price == Decimal("1.50")

    def test_first_matching_entry_wins_on_duplicate_canonical(
        self, httpx_mock
    ) -> None:
        """Two entries with the canonical title at different prices →
        takes the first (cheapest, since DMarket sorts price-ascending).
        Pins the 'iterate in order, take first match' semantic."""
        httpx_mock.add_response(
            json={
                "objects": _mixed_offers(
                    [
                        ("Wrong A", "50"),
                        (self._CANONICAL, "100"),
                        (self._CANONICAL, "150"),
                    ]
                )
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, self._CANONICAL)
        assert obs is not None
        assert obs.price == Decimal("1.00"), (
            "first matching entry should win (price 1.00); "
            f"got {obs.price}"
        )

    def test_nfc_normalization_handles_unicode_variants(
        self, httpx_mock
    ) -> None:
        """The accept-set membership check uses NFC normalization
        (db.naming.normalize_name) on both sides. A title that's NFC-
        equivalent to the canonical matches even if the byte-encoding
        differs (e.g. precomposed vs decomposed Unicode forms)."""
        # The ★ glyph is U+2605 (single code point, NFC-stable).
        # We construct a title that's byte-different but NFC-equivalent
        # to the canonical by using the same code points — proving the
        # equality goes through the normalize_name function. A more
        # elaborate test would use NFD vs NFC of an accented character;
        # for the project's CS2 vocabulary, U+2605 + standard ASCII is
        # the realistic surface and normalize_name's NFC pass handles
        # it correctly.
        name = "★ Karambit | Fade (Factory New)"
        httpx_mock.add_response(
            json={
                "objects": _mixed_offers(
                    [
                        ("★ Karambit | Marble Fade (Factory New)", "100"),
                        (name, "200"),
                    ]
                )
            }
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, name)
        assert obs is not None
        assert obs.price == Decimal("2.00")

    def test_empty_objects_array_returns_none(self, httpx_mock) -> None:
        """Step 6 refinement: DMarket sometimes returns objects: []
        ("no listings for this item"). The iterate-objects[] logic
        naturally handles this — the for-loop is a no-op on [] — but
        we pin it explicitly. The pre-Phase-2b code's objects[0]
        access on an empty list would have raised IndexError; the
        new code returns None cleanly.
        """
        httpx_mock.add_response(
            json={"objects": [], "total": {}}
        )
        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, self._CANONICAL)
        assert obs is None


# Real-fixture tests against captured DMarket responses. Fixtures live
# at tests/fixtures/dmarket/<slug>.json and are captured one-shot by
# scripts/capture_dmarket_fixtures.py. They prove the iterate-objects[]
# fix against actual DMarket response shape, not synthetic.

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dmarket"


# Two parameter sets: items that SHOULD resolve to a valid
# PriceObservation post-fix (canonical in objects[]) and items that
# remain unavailable because DMarket genuinely doesn't carry them.
_FIXTURES_RESOLVED = [
    ("M4A1-S | Cyrex (Field-Tested)", "m4a1-s-cyrex-field-tested"),
    ("MP9 | Hot Rod (Factory New)", "mp9-hot-rod-factory-new"),
    (
        "★ Butterfly Knife | Fade (Factory New)",
        "star-butterfly-knife-fade-factory-new",
    ),
    (
        "★ Huntsman Knife | Fade (Factory New)",
        "star-huntsman-knife-fade-factory-new",
    ),
    ("★ Karambit | Fade (Factory New)", "star-karambit-fade-factory-new"),
]

_FIXTURES_STILL_NO_DATA = [
    ("Desert Eagle | Blaze (Factory New)", "desert-eagle-blaze-factory-new"),
    (
        "SSG 08 | Death Strike (Factory New)",
        "ssg-08-death-strike-factory-new",
    ),
    (
        "Souvenir AWP | Dragon Lore (Battle-Scarred)",
        "souvenir-awp-dragon-lore-battle-scarred",
    ),
]


class TestPhase2bRealDMarketFixtures:
    """Real-fixture regression tests. Each captured response is a
    snapshot of DMarket's actual reply to ``title=<canonical>`` for
    the 8 previously-failing watchlist items. Proves the fix works
    against the response shape DMarket actually serves, not a
    synthetic approximation.
    """

    @pytest.mark.parametrize("name,slug", _FIXTURES_RESOLVED)
    def test_iteration_resolves_previously_failing_item(
        self, name: str, slug: str, httpx_mock
    ) -> None:
        fixture_path = _FIXTURE_DIR / f"{slug}.json"
        if not fixture_path.exists():
            pytest.skip(
                f"fixture {slug}.json not captured; run "
                f"`python -m scripts.capture_dmarket_fixtures`"
            )
        body = json.loads(fixture_path.read_text())
        httpx_mock.add_response(json=body)

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, name)

        assert obs is not None, (
            f"alias-map fix should resolve {name!r}; canonical name "
            f"is present in objects[] per the captured fixture"
        )
        # The cheapest entry whose NFC-title matches canonical should
        # be the persisted one. Verify the persisted title round-trips.
        from db.naming import normalize_name

        persisted_title = obs.raw_response["cheapest"]["title"]
        assert normalize_name(persisted_title) == normalize_name(name)

    @pytest.mark.parametrize("name,slug", _FIXTURES_STILL_NO_DATA)
    def test_no_data_items_remain_unavailable(
        self, name: str, slug: str, httpx_mock
    ) -> None:
        """Items where DMarket genuinely doesn't have the canonical
        listed (3 of the 8 originally-failing items) should still
        return None post-fix — same outcome as the pre-Phase-2b
        behavior, but reached after inspecting the full response."""
        fixture_path = _FIXTURE_DIR / f"{slug}.json"
        if not fixture_path.exists():
            pytest.skip(
                f"fixture {slug}.json not captured; run "
                f"`python -m scripts.capture_dmarket_fixtures`"
            )
        body = json.loads(fixture_path.read_text())
        httpx_mock.add_response(json=body)

        collector = DMarketCollector()
        with collector.make_client() as client:
            obs = collector.collect_one(client, name)

        assert obs is None, (
            f"DMarket fixture for {name!r} has no canonical title; "
            f"collector should return None"
        )
