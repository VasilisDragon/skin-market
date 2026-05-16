"""Tests for analytics/pattern_classifier.py.

All tests are pure-logic (no DB) thanks to the three-layer split:
- parse_pattern_yaml: pure file parsing.
- build_classifier: pure validation against pre-built sets.
- load_classifier: production wiring (covered indirectly via the
  layers above plus one fail-fast-inheritance test that exercises
  load_watchlist's tier validation).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from analytics.pattern_classifier import (
    _DEFAULT_ENTRY,
    _KNOWN_CATEGORIES,
    _SUPPORTED_SCHEMA_VERSION,
    ClassificationEntry,
    Classifier,
    _RawEntry,
    build_classifier,
    load_classifier,
    parse_pattern_yaml,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _write_yaml(tmp_path: Path, body: str, name: str = "pattern.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# A minimal valid pattern YAML body used as the baseline fixture.
_VALID_PATTERN_YAML = """\
schema_version: 1
items:
  - market_hash_name: "★ Karambit | Doppler (Factory New)"
    classification: phase_based
  - market_hash_name: "★ Sport Gloves | Vice (Field-Tested)"
    classification: pattern_seed
    drift_threshold_multiplier: 2.0
"""


# A minimal valid watchlist YAML the load_classifier integration test
# can read. Includes the items referenced in _VALID_PATTERN_YAML.
_VALID_WATCHLIST_YAML = """\
schema_version: 2

sources:
  - { name: skinport, base_url: https://example, rate_limit_per_minute: 60, enabled: true }

items:
  - { market_hash_name: "★ Karambit | Doppler (Factory New)", item_type: knife, tier: deep }
  - { market_hash_name: "★ Sport Gloves | Vice (Field-Tested)", item_type: glove, tier: deep }
"""


# ──────────────────────────────────────────────────────────────────────
# Layer 1 — parse_pattern_yaml: file + schema validation
# ──────────────────────────────────────────────────────────────────────


class TestParsePatternYaml:
    def test_parses_valid_yaml(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, _VALID_PATTERN_YAML)
        entries = parse_pattern_yaml(path)
        assert len(entries) == 2
        assert entries[0].market_hash_name == (
            "★ Karambit | Doppler (Factory New)"
        )
        assert entries[0].classification == "phase_based"
        assert entries[0].threshold_multiplier is None
        assert entries[1].classification == "pattern_seed"
        assert entries[1].threshold_multiplier == 2.0

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            parse_pattern_yaml(tmp_path / "missing.yaml")

    def test_rejects_unparseable_yaml(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "schema_version: 1\nitems: [unclosed")
        with pytest.raises(ValueError, match="unparseable"):
            parse_pattern_yaml(path)

    def test_rejects_top_level_not_mapping(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "- item1\n- item2\n")
        with pytest.raises(ValueError, match="top level must be a mapping"):
            parse_pattern_yaml(path)

    def test_rejects_missing_schema_version(self, tmp_path: Path) -> None:
        """Step 4 refinement: a YAML without the schema_version key
        must fail fast with a clear message. Someone copy-pasting
        from the file's docstring six months from now might forget
        to include the field; the loader protects them with an
        explicit error."""
        path = _write_yaml(
            tmp_path,
            "items:\n  - { market_hash_name: \"X (FN)\", classification: phase_based }\n",
        )
        with pytest.raises(
            ValueError, match="missing required `schema_version:`"
        ):
            parse_pattern_yaml(path)

    def test_rejects_wrong_schema_version(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
schema_version: 2
items:
  - { market_hash_name: "X (FN)", classification: phase_based }
""",
        )
        with pytest.raises(ValueError, match="schema_version is 2"):
            parse_pattern_yaml(path)

    def test_rejects_unknown_classification(self, tmp_path: Path) -> None:
        """Step 4 refinement: assert the error message contains at
        least three known category names, so an operator typo gets a
        useful hint and a future refactor doesn't strip the list
        without anyone noticing."""
        path = _write_yaml(
            tmp_path,
            """\
schema_version: 1
items:
  - { market_hash_name: "X (FN)", classification: foobar }
""",
        )
        with pytest.raises(ValueError) as exc_info:
            parse_pattern_yaml(path)
        msg = str(exc_info.value)
        assert "foobar" in msg
        # The known-category list must surface in the message —
        # operator-debugging UX depends on it.
        assert "phase_based" in msg
        assert "pattern_seed" in msg
        assert "pattern_agnostic" in msg

    def test_rejects_explicit_pattern_agnostic(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
schema_version: 1
items:
  - { market_hash_name: "X (FN)", classification: pattern_agnostic }
""",
        )
        with pytest.raises(
            ValueError, match="implicit default; remove this entry"
        ):
            parse_pattern_yaml(path)

    def test_rejects_duplicate_market_hash_name(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
schema_version: 1
items:
  - { market_hash_name: "X (FN)", classification: phase_based }
  - { market_hash_name: "X (FN)", classification: pattern_seed }
""",
        )
        with pytest.raises(ValueError, match="duplicate market_hash_name"):
            parse_pattern_yaml(path)

    def test_accepts_extensible_categories(self, tmp_path: Path) -> None:
        """The four documented-but-not-seeded categories must
        validate without complaint — they're forward-compat slots."""
        for category in (
            "pattern_ranked",
            "wear_percentage",
            "sticker_premium",
            "float_sensitive",
        ):
            path = _write_yaml(
                tmp_path,
                f"""\
schema_version: 1
items:
  - {{ market_hash_name: "X (FN)", classification: {category} }}
""",
                name=f"pattern_{category}.yaml",
            )
            entries = parse_pattern_yaml(path)
            assert entries[0].classification == category

    def test_rejects_non_numeric_multiplier(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
schema_version: 1
items:
  - market_hash_name: "X (FN)"
    classification: pattern_seed
    drift_threshold_multiplier: "two"
""",
        )
        with pytest.raises(ValueError, match="drift_threshold_multiplier"):
            parse_pattern_yaml(path)

    def test_rejects_negative_multiplier(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """\
schema_version: 1
items:
  - market_hash_name: "X (FN)"
    classification: pattern_seed
    drift_threshold_multiplier: -0.5
""",
        )
        with pytest.raises(ValueError, match="must be positive"):
            parse_pattern_yaml(path)


# ──────────────────────────────────────────────────────────────────────
# Layer 2 — build_classifier: cross-reference validation
# ──────────────────────────────────────────────────────────────────────


def _raw(
    name: str,
    classification: str,
    *,
    multiplier: float | None = None,
    note: str | None = None,
) -> _RawEntry:
    return _RawEntry(
        market_hash_name=name,
        classification=classification,
        threshold_multiplier=multiplier,
        note=note,
    )


class TestBuildClassifier:
    def test_builds_with_valid_entries(self) -> None:
        raw = [
            _raw("A (FN)", "phase_based"),
            _raw("B (FT)", "pattern_seed", multiplier=2.0),
        ]
        items = {"A (FN)", "B (FT)"}
        deep = items
        cls = build_classifier(raw, items_set=items, deep_set=deep)
        assert len(cls) == 2
        assert cls.is_phase_based("A (FN)")
        assert cls.threshold_multiplier_for("B (FT)") == 2.0

    def test_fails_fast_on_unknown_item(self) -> None:
        """Named mode 1: market_hash_name not in items_set."""
        raw = [_raw("Missing (FN)", "phase_based")]
        with pytest.raises(ValueError, match="not in items table"):
            build_classifier(raw, items_set=set(), deep_set=set())

    def test_fails_fast_on_broad_tier_item(self) -> None:
        """Named mode 2: market_hash_name in items_set but NOT in
        deep_set (i.e. tier: broad)."""
        raw = [_raw("Broad Item (FN)", "phase_based")]
        with pytest.raises(ValueError, match="tier: broad"):
            build_classifier(
                raw,
                items_set={"Broad Item (FN)"},
                deep_set=set(),
            )

    def test_warns_once_on_phase_based_with_multiplier(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Step 4 refinement (test 12): assert THREE things — WARN
        logged once AND the multiplier is dropped to 1.0 in BOTH
        threshold_multiplier_for() AND get().threshold_multiplier.
        Without the explicit drop assertions, a future refactor that
        preserves the multiplier on the entry while still logging
        the WARN would pass the test silently — defeating the whole
        point of the WARN-once contract.
        """
        raw = [_raw("Phase Item (FN)", "phase_based", multiplier=2.5)]
        with caplog.at_level(
            logging.WARNING, logger="analytics.pattern_classifier"
        ):
            cls = build_classifier(
                raw,
                items_set={"Phase Item (FN)"},
                deep_set={"Phase Item (FN)"},
            )

        # 1. WARN logged once.
        warns = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warns) == 1, (
            f"expected exactly 1 WARN, got {len(warns)}: {warns}"
        )
        assert "phase_based" in warns[0].getMessage().lower()
        assert "dead config" in warns[0].getMessage().lower()

        # 2. threshold_multiplier_for returns 1.0 (the dropped value).
        assert cls.threshold_multiplier_for("Phase Item (FN)") == 1.0

        # 3. The underlying entry's multiplier is also 1.0 — pins the
        # invariant against a refactor that decouples the public
        # method from the stored entry.
        assert cls.get("Phase Item (FN)").threshold_multiplier == 1.0

    def test_no_warn_on_phase_based_without_multiplier(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARN fires ONLY when a multiplier is explicitly set
        on a phase_based item. A phase_based item without a
        multiplier is clean config and must not log."""
        raw = [_raw("Phase Clean (FN)", "phase_based", multiplier=None)]
        with caplog.at_level(
            logging.WARNING, logger="analytics.pattern_classifier"
        ):
            build_classifier(
                raw,
                items_set={"Phase Clean (FN)"},
                deep_set={"Phase Clean (FN)"},
            )
        warns = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warns == []

    def test_default_multiplier_one_when_field_absent(self) -> None:
        raw = [_raw("X (FN)", "pattern_seed", multiplier=None)]
        cls = build_classifier(
            raw, items_set={"X (FN)"}, deep_set={"X (FN)"}
        )
        assert cls.threshold_multiplier_for("X (FN)") == 1.0


# ──────────────────────────────────────────────────────────────────────
# Layer 3 — load_classifier: file integration
# ──────────────────────────────────────────────────────────────────────


class TestLoadClassifierIntegration:
    def test_inherits_watchlist_fail_fast_on_missing_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Named mode 3: a watchlist item missing the required tier
        field causes load_watchlist to fail fast; load_classifier
        propagates the error cleanly."""
        pattern_path = _write_yaml(
            tmp_path, _VALID_PATTERN_YAML, name="pattern.yaml"
        )
        # Invalid watchlist: one item lacks the tier field.
        bad_watchlist = (
            "schema_version: 2\n"
            "sources:\n"
            "  - { name: skinport, base_url: https://example, "
            "rate_limit_per_minute: 60, enabled: true }\n"
            "items:\n"
            '  - { market_hash_name: "X (FN)", item_type: knife }\n'
        )
        watchlist_path = _write_yaml(
            tmp_path, bad_watchlist, name="watchlist.yaml"
        )

        # The watchlist load fails before any DB call — no session
        # required.
        with pytest.raises(ValueError, match="missing required `tier:`"):
            load_classifier(
                pattern_path,
                watchlist_path=watchlist_path,
            )

    def test_propagates_pattern_yaml_errors(self, tmp_path: Path) -> None:
        """Errors from parse_pattern_yaml propagate through
        load_classifier cleanly — no swallowing or re-wrapping."""
        pattern_path = _write_yaml(
            tmp_path,
            "schema_version: 99\nitems: []\n",
            name="pattern.yaml",
        )
        watchlist_path = _write_yaml(
            tmp_path, _VALID_WATCHLIST_YAML, name="watchlist.yaml"
        )
        with pytest.raises(ValueError, match="schema_version is 99"):
            load_classifier(
                pattern_path,
                watchlist_path=watchlist_path,
            )


# ──────────────────────────────────────────────────────────────────────
# Classifier public API
# ──────────────────────────────────────────────────────────────────────


def _seeded_classifier() -> Classifier:
    """Build a Classifier from a small canonical seed for API tests."""
    raw = [
        _raw("Doppler FN", "phase_based"),
        _raw("Vice FT", "pattern_seed", multiplier=2.0),
    ]
    items = {"Doppler FN", "Vice FT", "Random FN"}
    deep = items
    return build_classifier(raw, items_set=items, deep_set=deep)


class TestClassifierApi:
    def test_get_returns_entry_for_known_item(self) -> None:
        cls = _seeded_classifier()
        entry = cls.get("Doppler FN")
        assert entry.classification == "phase_based"

    def test_get_returns_default_for_unknown_item(self) -> None:
        cls = _seeded_classifier()
        entry = cls.get("Unknown FN")
        assert entry.classification == "pattern_agnostic"
        assert entry.threshold_multiplier == 1.0
        assert entry.note is None

    def test_is_phase_based_true_for_phase_based_item(self) -> None:
        cls = _seeded_classifier()
        assert cls.is_phase_based("Doppler FN") is True

    def test_is_phase_based_false_for_pattern_seed_item(self) -> None:
        cls = _seeded_classifier()
        assert cls.is_phase_based("Vice FT") is False

    def test_is_phase_based_false_for_unknown_item(self) -> None:
        cls = _seeded_classifier()
        assert cls.is_phase_based("Anything Random (FN)") is False

    def test_threshold_multiplier_for_pattern_seed(self) -> None:
        cls = _seeded_classifier()
        assert cls.threshold_multiplier_for("Vice FT") == 2.0

    def test_threshold_multiplier_for_phase_based_returns_one(self) -> None:
        """phase_based items get 1.0 even though the multiplier is
        dead config; the drift detector skips them entirely and
        never reads it. Defaulting to 1.0 keeps the API uniform."""
        cls = _seeded_classifier()
        assert cls.threshold_multiplier_for("Doppler FN") == 1.0

    def test_threshold_multiplier_for_unknown_item(self) -> None:
        cls = _seeded_classifier()
        assert cls.threshold_multiplier_for("Unknown FN") == 1.0

    def test_contains_true_for_explicit_entries(self) -> None:
        cls = _seeded_classifier()
        assert "Doppler FN" in cls

    def test_contains_false_for_default_items(self) -> None:
        cls = _seeded_classifier()
        assert "Unknown FN" not in cls

    def test_len_matches_explicit_entries(self) -> None:
        cls = _seeded_classifier()
        assert len(cls) == 2

    def test_classifier_immutable(self) -> None:
        """MappingProxyType prevents mutation of the entries dict via
        the Classifier. Verifies the immutability invariant."""
        cls = _seeded_classifier()
        with pytest.raises(TypeError):
            # MappingProxyType raises TypeError on __setitem__.
            cls._entries["x"] = ClassificationEntry(  # type: ignore[index]
                "phase_based", 1.0, None
            )


# ──────────────────────────────────────────────────────────────────────
# Seed-file sanity: data/pattern_sensitivity.yaml parses cleanly
# ──────────────────────────────────────────────────────────────────────


class TestSeededYamlFile:
    """Verify the actually-committed data/pattern_sensitivity.yaml
    parses and matches the documented seed of 6 entries (3 Dopplers
    + Marble Fade + Tiger Tooth + Sport Gloves Vice)."""

    def test_seeded_file_parses(self) -> None:
        from analytics.pattern_classifier import DEFAULT_PATTERN_PATH

        entries = parse_pattern_yaml(DEFAULT_PATTERN_PATH)
        assert len(entries) == 6
        names = {e.market_hash_name for e in entries}
        assert "★ Karambit | Doppler (Factory New)" in names
        assert "★ M9 Bayonet | Doppler (Factory New)" in names
        assert "★ Flip Knife | Doppler (Factory New)" in names
        assert "★ Karambit | Marble Fade (Factory New)" in names
        assert "★ Karambit | Tiger Tooth (Factory New)" in names
        assert "★ Sport Gloves | Vice (Field-Tested)" in names

    def test_seeded_phase_based_count(self) -> None:
        from analytics.pattern_classifier import DEFAULT_PATTERN_PATH

        entries = parse_pattern_yaml(DEFAULT_PATTERN_PATH)
        phase_based = [
            e for e in entries if e.classification == "phase_based"
        ]
        assert len(phase_based) == 5

    def test_seeded_pattern_seed_count(self) -> None:
        from analytics.pattern_classifier import DEFAULT_PATTERN_PATH

        entries = parse_pattern_yaml(DEFAULT_PATTERN_PATH)
        seed_entries = [
            e for e in entries if e.classification == "pattern_seed"
        ]
        assert len(seed_entries) == 1
        assert seed_entries[0].threshold_multiplier == 2.0


# ──────────────────────────────────────────────────────────────────────
# Sanity: module constants
# ──────────────────────────────────────────────────────────────────────


def test_module_constants_present() -> None:
    """Module exports the documented constants. Protects against an
    accidental rename."""
    assert _SUPPORTED_SCHEMA_VERSION == 1
    assert "phase_based" in _KNOWN_CATEGORIES
    assert "pattern_seed" in _KNOWN_CATEGORIES
    assert "pattern_agnostic" in _KNOWN_CATEGORIES
    assert _DEFAULT_ENTRY.classification == "pattern_agnostic"
    assert _DEFAULT_ENTRY.threshold_multiplier == 1.0
    assert _DEFAULT_ENTRY.note is None
