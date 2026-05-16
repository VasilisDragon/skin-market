"""Pattern-sensitivity classifier loader (Phase 2b, ADR 021).

Reads ``data/pattern_sensitivity.yaml`` and exposes a queryable
in-memory view: "what classification applies to this market_hash_name?"
Items not in the YAML default to ``pattern_agnostic`` with multiplier
1.0.

Deploy-order dependency: ``data/watchlist.yaml`` changes and
``scripts/seed_watchlist.py`` must deploy BEFORE this file is loaded.
The loader cross-references each entry against:

1. The ``items`` table — catches typos / unseeded entries.
2. The ``watchlist.yaml`` tier field — catches a broad-tier item
   accidentally gaining a classification.

Reversing the order fails fast at startup with one of:

- ``"market_hash_name not in items table"``
- ``"market_hash_name has tier: broad — drift detection is deep-only"``
- ``"missing required `tier:` field"`` (inherited from load_watchlist)

YAML schema versions are PER-FILE. This file's ``schema_version: 1``
is independent of ``data/watchlist.yaml``'s ``schema_version: 2``.
Bumping one does not imply bumping the other.

Three-layer architecture for testability:

- ``parse_pattern_yaml(path)`` — pure file parsing + schema validation.
  No DB or watchlist cross-references.
- ``build_classifier(raw_entries, items_set, deep_set, *, logger)`` —
  pure cross-reference validation against pre-built sets. Used by
  tests to skip DB / watchlist setup entirely.
- ``load_classifier(pattern_path, *, watchlist_path, session, logger)``
  — production entry point that wires file + DB + watchlist.

Categories (seven defined; three seeded — see
``data/pattern_sensitivity.yaml`` header):

  phase_based      Upstream taxonomy aggregates phases under one
                   market_hash_name. Drift is meaningless; the
                   detector emits pattern_skip (no number).
  pattern_seed     Specific paint-seed patterns drive listing-level
                   price; elevated drift_threshold_multiplier.
  pattern_agnostic IMPLICIT default for items not listed. Rejected if
                   used explicitly in the YAML (dead config).
  pattern_ranked   (extensible, not seeded) Tier-ranked patterns
                   like Case Hardened.
  wear_percentage  (extensible, not seeded) Float-driven pricing.
  sticker_premium  (extensible, not seeded) Sticker craft premium.
  float_sensitive  (extensible, not seeded) Extreme-float dominance.

WARN-once behavior: an entry whose classification is ``phase_based``
AND that carries a ``drift_threshold_multiplier`` is dead config (the
multiplier is ignored for phase_based items; they emit pattern_skip
unconditionally). The loader logs a single WARN at load time and
silently drops the multiplier to 1.0 in the resulting classifier.
There is no per-cycle log noise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.connection import get_engine
from scripts.seed_watchlist import load_watchlist

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATTERN_PATH = _REPO_ROOT / "data" / "pattern_sensitivity.yaml"
DEFAULT_WATCHLIST_PATH = _REPO_ROOT / "data" / "watchlist.yaml"

_SUPPORTED_SCHEMA_VERSION = 1

# Known classification values. New categories: add here AND update
# data/pattern_sensitivity.yaml header. pattern_agnostic is included
# so that parse-time validation can reject explicit pattern_agnostic
# entries with a clear "use the implicit default" message — see
# parse_pattern_yaml.
_KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "phase_based",
        "pattern_seed",
        "pattern_agnostic",
        "pattern_ranked",
        "wear_percentage",
        "sticker_premium",
        "float_sensitive",
    }
)


# ──────────────────────────────────────────────────────────────────────
# Public data shapes
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassificationEntry:
    """One item's classification. ``threshold_multiplier`` is the
    coefficient applied to the baseline drift threshold (1.0 = baseline,
    2.0 = doubled, etc.); for phase_based items the value is always
    1.0 because the drift detector skips them entirely.

    ``note`` is operator-facing rationale carried through from the
    YAML. Never read by code; useful for a future "explain this
    verdict" path."""

    classification: str
    threshold_multiplier: float
    note: str | None


# Implicit default returned for any market_hash_name not in the
# explicit exception list. Frozen + module-level so callers can rely
# on identity (``entry is _DEFAULT_ENTRY``) if useful, though normal
# code paths just check ``entry.classification``.
_DEFAULT_ENTRY = ClassificationEntry(
    classification="pattern_agnostic",
    threshold_multiplier=1.0,
    note=None,
)


@dataclass(frozen=True)
class _RawEntry:
    """One parsed entry from pattern_sensitivity.yaml, post-structural-
    validation but pre-DB / pre-watchlist cross-reference. Internal —
    callers shouldn't reach for this type."""

    market_hash_name: str
    classification: str
    threshold_multiplier: float | None  # None = field absent from YAML
    note: str | None


class Classifier:
    """Immutable classifier. Construct via ``build_classifier()`` or
    ``load_classifier()``; do not instantiate directly outside the
    loader module.

    Lookups via ``.get(name)`` return the explicit entry if present,
    or ``_DEFAULT_ENTRY`` (pattern_agnostic, multiplier 1.0) as a
    fallback. The instance wraps its internal mapping in
    ``MappingProxyType`` so callers can't mutate the entries dict.
    """

    __slots__ = ("_entries",)

    def __init__(
        self, entries: Mapping[str, ClassificationEntry]
    ) -> None:
        # MappingProxyType prevents mutation through the proxy; copy
        # the input dict so future caller-side mutation can't leak in
        # either.
        object.__setattr__(
            self, "_entries", MappingProxyType(dict(entries))
        )

    def get(self, market_hash_name: str) -> ClassificationEntry:
        """Explicit entry if present, else the pattern_agnostic default."""
        return self._entries.get(market_hash_name, _DEFAULT_ENTRY)

    def is_phase_based(self, market_hash_name: str) -> bool:
        """Sugar for ``self.get(name).classification == 'phase_based'``
        — the drift detector's main branch."""
        return self.get(market_hash_name).classification == "phase_based"

    def threshold_multiplier_for(self, market_hash_name: str) -> float:
        """Sugar for ``self.get(name).threshold_multiplier``."""
        return self.get(market_hash_name).threshold_multiplier

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, market_hash_name: object) -> bool:
        return market_hash_name in self._entries


# ──────────────────────────────────────────────────────────────────────
# Layer 1: parse_pattern_yaml — pure file parsing + schema validation
# ──────────────────────────────────────────────────────────────────────


def parse_pattern_yaml(path: Path) -> list[_RawEntry]:
    """Read, parse, and structurally validate
    ``data/pattern_sensitivity.yaml``. Returns one ``_RawEntry`` per
    item in the file's ``items:`` list.

    No DB access, no cross-reference against watchlist.yaml. Pure
    file-level validation.

    Fail-fast on:
    - File missing → ``FileNotFoundError``.
    - YAML unparseable → ``ValueError`` wrapping the YAML error.
    - Top-level not a mapping → ``ValueError``.
    - ``schema_version`` absent or != 1 → ``ValueError``.
    - ``items`` key absent or not a list → ``ValueError``.
    - Any item not a mapping → ``ValueError``.
    - Missing ``market_hash_name`` on any item → ``ValueError``.
    - Missing ``classification`` on any item → ``ValueError``.
    - ``classification`` value not in the seven known categories →
      ``ValueError`` whose message lists at least three known values
      so an operator typo gets a useful hint.
    - ``classification: pattern_agnostic`` explicitly set →
      ``ValueError`` ("implicit default; remove entry").
    - ``drift_threshold_multiplier`` not a positive number →
      ``ValueError``.
    - Duplicate ``market_hash_name`` entries → ``ValueError``.
    """
    if not path.exists():
        raise FileNotFoundError(f"pattern_sensitivity.yaml not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: unparseable YAML — {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top level must be a mapping, "
            f"got {type(data).__name__}"
        )

    if "schema_version" not in data:
        raise ValueError(
            f"{path}: missing required `schema_version:` key. "
            f"Expected schema_version: {_SUPPORTED_SCHEMA_VERSION}."
        )
    version = data["schema_version"]
    if version != _SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version is {version!r}, expected "
            f"{_SUPPORTED_SCHEMA_VERSION}. Update the loader or the YAML."
        )

    items = data.get("items")
    if items is None:
        raise ValueError(f"{path}: missing required `items:` key")
    if not isinstance(items, list):
        raise ValueError(
            f"{path}: `items:` must be a list, got {type(items).__name__}"
        )

    seen_names: set[str] = set()
    raw_entries: list[_RawEntry] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"{path}: items[{idx}] must be a mapping, "
                f"got {type(item).__name__}"
            )

        name = item.get("market_hash_name")
        if not name or not isinstance(name, str):
            raise ValueError(
                f"{path}: items[{idx}] is missing market_hash_name "
                f"or it isn't a non-empty string"
            )

        if name in seen_names:
            raise ValueError(
                f"{path}: duplicate market_hash_name {name!r}"
            )
        seen_names.add(name)

        cls = item.get("classification")
        if not cls or not isinstance(cls, str):
            raise ValueError(
                f"{path}: items[{idx}] ({name!r}) is missing "
                f"classification or it isn't a string"
            )
        if cls not in _KNOWN_CATEGORIES:
            # Sorted+joined explicitly so the message lists every
            # known category — protects operator debugging UX.
            known_list = ", ".join(sorted(_KNOWN_CATEGORIES))
            raise ValueError(
                f"{path}: items[{idx}] ({name!r}) has unknown "
                f"classification {cls!r}. Known categories: "
                f"{known_list}."
            )
        if cls == "pattern_agnostic":
            raise ValueError(
                f"{path}: items[{idx}] ({name!r}) sets "
                f"classification: pattern_agnostic explicitly. "
                f"pattern_agnostic is the implicit default; remove "
                f"this entry to use it."
            )

        raw_multiplier: Any = item.get("drift_threshold_multiplier")
        multiplier: float | None = None
        if raw_multiplier is not None:
            if isinstance(raw_multiplier, bool) or not isinstance(
                raw_multiplier, (int, float)
            ):
                raise ValueError(
                    f"{path}: items[{idx}] ({name!r}) has "
                    f"drift_threshold_multiplier of type "
                    f"{type(raw_multiplier).__name__}; expected a "
                    f"positive number."
                )
            if raw_multiplier <= 0:
                raise ValueError(
                    f"{path}: items[{idx}] ({name!r}) has "
                    f"drift_threshold_multiplier <= 0 "
                    f"({raw_multiplier!r}); must be positive."
                )
            multiplier = float(raw_multiplier)

        note: str | None = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError(
                f"{path}: items[{idx}] ({name!r}) has note of type "
                f"{type(note).__name__}; expected string."
            )

        raw_entries.append(
            _RawEntry(
                market_hash_name=name,
                classification=cls,
                threshold_multiplier=multiplier,
                note=note,
            )
        )

    return raw_entries


# ──────────────────────────────────────────────────────────────────────
# Layer 2: build_classifier — pure cross-reference validation
# ──────────────────────────────────────────────────────────────────────


def build_classifier(
    raw_entries: list[_RawEntry],
    *,
    items_set: set[str],
    deep_set: set[str],
    log: logging.Logger | None = None,
) -> Classifier:
    """Cross-reference each raw entry against ``items_set`` and
    ``deep_set``, then build the final ``Classifier``.

    Pure function. Tests inject synthetic sets to skip DB and
    watchlist setup entirely.

    Named fail-fast modes (Step 4 brief):

    1. UNKNOWN ITEM — ``market_hash_name`` not in ``items_set``.
       ``ValueError`` with "not in items table" wording so the
       operator can locate the typo.
    2. BROAD-TIER ITEM — ``market_hash_name`` in ``items_set`` but
       NOT in ``deep_set`` (i.e. flagged ``tier: broad`` in
       watchlist.yaml). ``ValueError`` with "tier: broad" wording.
    3. MISSING TIER FIELD — inherited upstream from
       ``load_watchlist``, which fail-fasts before this function
       sees the data. This function never sees a tier-less item.

    WARN-once behavior: a phase_based entry with a non-None
    ``threshold_multiplier`` is dead config (the drift detector
    skips phase_based items entirely; the multiplier never gets
    consulted). The function logs a single WARN per offending entry
    and silently drops the multiplier to 1.0 in the resulting
    ``ClassificationEntry``. Subsequent ``.get()`` calls return
    multiplier=1.0; there's no per-cycle log noise.
    """
    log = log or logger
    built: dict[str, ClassificationEntry] = {}

    for raw in raw_entries:
        if raw.market_hash_name not in items_set:
            raise ValueError(
                f"pattern_sensitivity: {raw.market_hash_name!r} not "
                f"in items table. Ensure data/watchlist.yaml + "
                f"scripts/seed_watchlist.py have been run before "
                f"loading the classifier."
            )
        if raw.market_hash_name not in deep_set:
            raise ValueError(
                f"pattern_sensitivity: {raw.market_hash_name!r} has "
                f"tier: broad in data/watchlist.yaml — drift "
                f"detection is deep-only. Move the item to tier: "
                f"deep or remove the classifier entry."
            )

        # Resolve threshold_multiplier with the phase_based WARN-once
        # rule applied.
        if raw.classification == "phase_based":
            if raw.threshold_multiplier is not None:
                log.warning(
                    "pattern_sensitivity: %r is phase_based AND "
                    "carries drift_threshold_multiplier=%s. Dead "
                    "config — phase_based items emit pattern_skip "
                    "regardless of multiplier. Dropping multiplier "
                    "from the classifier; remove the field from "
                    "data/pattern_sensitivity.yaml to clear this "
                    "warning.",
                    raw.market_hash_name,
                    raw.threshold_multiplier,
                )
            effective_multiplier = 1.0
        else:
            effective_multiplier = (
                raw.threshold_multiplier
                if raw.threshold_multiplier is not None
                else 1.0
            )

        built[raw.market_hash_name] = ClassificationEntry(
            classification=raw.classification,
            threshold_multiplier=effective_multiplier,
            note=raw.note,
        )

    return Classifier(built)


# ──────────────────────────────────────────────────────────────────────
# Layer 3: load_classifier — production entry point
# ──────────────────────────────────────────────────────────────────────


def _query_items_set(session: Session) -> set[str]:
    """Return the set of every market_hash_name currently in the
    items table."""
    rows = session.execute(
        text("SELECT market_hash_name FROM items")
    ).all()
    return {r.market_hash_name for r in rows}


def load_classifier(
    pattern_path: Path = DEFAULT_PATTERN_PATH,
    *,
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
    session: Session | None = None,
    log: logging.Logger | None = None,
) -> Classifier:
    """Production entry point: load ``pattern_sensitivity.yaml``,
    cross-reference against the items table + watchlist tier info,
    return a built ``Classifier``.

    If ``session`` is None, opens its own session via ``get_engine()``
    — matches the existing pattern in seed_watchlist.py. Tests pass
    an explicit session for isolation; production callers omit.

    Deploy-order dependency documented in the module docstring.
    Inherits load_watchlist's "missing tier field" fail-fast.
    """
    log = log or logger
    raw_entries = parse_pattern_yaml(pattern_path)

    # load_watchlist validates schema_version 2 + per-item tier field
    # — its fail-fast on missing tier is the third named mode for
    # this loader, inherited cleanly.
    watchlist_data = load_watchlist(watchlist_path)
    deep_set = {
        it["market_hash_name"]
        for it in watchlist_data["items"]
        if it.get("tier") == "deep"
    }

    if session is None:
        with Session(get_engine()) as s:
            items_set = _query_items_set(s)
    else:
        items_set = _query_items_set(session)

    return build_classifier(
        raw_entries,
        items_set=items_set,
        deep_set=deep_set,
        log=log,
    )
