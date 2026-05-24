"""Capture live DMarket alias-map fixtures.

Writes live responses to ``tests/fixtures/dmarket/`` so DMarket title
matching tests exercise real response shapes rather than synthetic bodies.

Usage:
    uv run python -m scripts.capture_dmarket_fixtures

For each item, prints a summary listing the unique titles found in
``objects[]``, with a ``✓`` next to any title that matches the
canonical name (NFC-normalized). Use these summaries to decide what
``dmarket_alias`` values, if any, to add to ``data/watchlist.yaml``.
If all items have the canonical name
somewhere in ``objects[]``, no aliases are needed — the
iterate-objects[] fix alone resolves the problem; the alias field
stays empty / absent for those items.

Idempotency: if a fixture already exists with identical content to
the new response, the script skips silently. If it exists with
DIFFERENT content, the script prompts for explicit overwrite —
belt-and-braces against accidentally invalidating a passing test.

429 handling: full-jitter backoff with up to 5 retries per item.
Pauses and resumes; does not exit halfway with partial fixtures.
Re-running the script is safe (idempotent skip on already-captured
items).

Re-capture manually if DMarket's response shape shifts and a fixture
test breaks. This is not part of the normal test cycle.

Point-in-time evidence, not eternal truth: the captured fixtures
reflect DMarket's response shape AND its catalog composition at
capture time. Re-run capture if either changes — DMarket may add
or remove a variant for a given title, shift which variant sits at
which index, or change which items it carries. The fixtures pin the
RESPONSE SHAPE the collector parses, not a claim about DMarket's
permanent catalog. ADR 012 §7's empirical-findings table is similarly
point-in-time; refresh both together when re-capturing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from collectors.base import (
    DEFAULT_USER_AGENT,
    full_jitter_backoff,
    parse_retry_after,
)
from collectors.dmarket import (
    DMARKET_BASE_URL,
    DMARKET_GAME_ID_CS2,
)
from db.naming import normalize_name, slugify

_FAILING_ITEMS: list[str] = [
    "Desert Eagle | Blaze (Factory New)",
    "M4A1-S | Cyrex (Field-Tested)",
    "MP9 | Hot Rod (Factory New)",
    "SSG 08 | Death Strike (Factory New)",
    "Souvenir AWP | Dragon Lore (Battle-Scarred)",
    "★ Butterfly Knife | Fade (Factory New)",
    "★ Huntsman Knife | Fade (Factory New)",
    "★ Karambit | Fade (Factory New)",
]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "dmarket"
_INTER_REQUEST_DELAY = 3.0  # matches collectors/dmarket.py
_MAX_RETRIES = 5
_HTTP_TIMEOUT = 30.0


def _fetch_one(client: httpx.Client, name: str) -> dict[str, Any]:
    """Hit DMarket for one item, retrying on 429 with full-jitter
    backoff. Raises RuntimeError if all retries exhaust."""
    for attempt in range(_MAX_RETRIES):
        response = client.get(
            DMARKET_BASE_URL,
            params={
                "gameId": DMARKET_GAME_ID_CS2,
                "title": name,
                "currency": "USD",
                "limit": "100",
                "orderBy": "price",
                "orderDir": "asc",
            },
        )
        if response.status_code == 429:
            retry_after = parse_retry_after(
                response.headers.get("Retry-After")
            )
            if retry_after is not None:
                delay = float(min(retry_after, 60))
            else:
                delay = full_jitter_backoff(attempt)
            print(
                f"  429 — sleeping {delay:.1f}s before retry "
                f"{attempt + 2}/{_MAX_RETRIES}"
            )
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(
        f"Capture failed after {_MAX_RETRIES} retries for {name!r}"
    )


def _summarize_titles(body: dict[str, Any]) -> list[str]:
    """Unique titles from ``body['objects']`` preserving order. The
    output informs optional ``dmarket_alias`` entries."""
    objects = body.get("objects") or []
    seen: set[str] = set()
    ordered: list[str] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        title = obj.get("title")
        if isinstance(title, str) and title not in seen:
            seen.add(title)
            ordered.append(title)
    return ordered


def _should_write(
    path: Path, new_body: dict[str, Any]
) -> tuple[bool, str]:
    """Decide whether to write ``new_body`` to ``path``.

    Returns ``(should_write, status_reason)`` where status_reason is
    one of:
    - "new": file doesn't exist; write.
    - "identical": file matches new content; skip.
    - "user_confirmed": file differed; user said yes.
    - "user_declined": file differed; user said no.
    - "corrupted_overwrite": file exists but isn't valid JSON; write.
    """
    if not path.exists():
        return True, "new"
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError:
        print(
            f"  ⚠ Existing {path.name} is not valid JSON; "
            f"will overwrite."
        )
        return True, "corrupted_overwrite"
    if existing == new_body:
        return False, "identical"
    print(
        f"  ⚠ {path.name} exists with DIFFERENT content from this "
        f"capture. Overwriting will invalidate any test that depends "
        f"on the previously-captured response."
    )
    answer = input(f"    Overwrite {path.name}? [y/N] ").strip().lower()
    return (answer == "y"), (
        "user_confirmed" if answer == "y" else "user_declined"
    )


def main() -> int:
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }

    written = 0
    skipped_identical = 0
    skipped_declined = 0
    failed: list[str] = []

    with httpx.Client(
        headers=headers, timeout=httpx.Timeout(_HTTP_TIMEOUT)
    ) as client:
        for idx, name in enumerate(_FAILING_ITEMS):
            slug = slugify(name)
            path = _FIXTURE_DIR / f"{slug}.json"
            print(
                f"\n[{idx + 1}/{len(_FAILING_ITEMS)}] Fetching {name!r}"
            )

            try:
                body = _fetch_one(client, name)
            except Exception as exc:
                print(f"  ✗ Capture failed: {exc}")
                failed.append(name)
                # Still pace before the next item so we don't burst
                # against an upstream that's already unhappy.
                if idx < len(_FAILING_ITEMS) - 1:
                    time.sleep(_INTER_REQUEST_DELAY)
                continue

            titles = _summarize_titles(body)
            total_objects = len(body.get("objects") or [])
            print(
                f"  objects[] returned {total_objects} entries; "
                f"{len(titles)} unique titles:"
            )
            canonical = normalize_name(name)
            for t in titles[:10]:
                marker = " ✓" if normalize_name(t) == canonical else ""
                print(f"    - {t}{marker}")
            if len(titles) > 10:
                print(f"    ... and {len(titles) - 10} more")

            should_write, reason = _should_write(path, body)
            if not should_write:
                if reason == "identical":
                    print(f"  · {path.name} unchanged; skipped.")
                    skipped_identical += 1
                else:
                    print(f"  · {path.name} not overwritten ({reason}).")
                    skipped_declined += 1
            else:
                path.write_text(json.dumps(body, indent=2))
                print(f"  ✓ Wrote {path.name} ({reason}).")
                written += 1

            if idx < len(_FAILING_ITEMS) - 1:
                time.sleep(_INTER_REQUEST_DELAY)

    print(
        f"\nCapture complete: {written} written, "
        f"{skipped_identical} unchanged, {skipped_declined} declined, "
        f"{len(failed)} failed."
    )
    if failed:
        print(f"Failed items: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
