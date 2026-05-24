"""Scheduler service for the v1 collection layer.

Runs as the foreground process of the ``collector`` Docker service.
DB-driven (ADR 013): on startup the scheduler queries
``SELECT … FROM sources WHERE enabled = TRUE`` and registers one
APScheduler job per enabled source, with cadence (``interval_minutes``,
``per_item_delay_seconds``) read from the same row. ``sources.enabled``
is the single switch for "do not poll this source" — same flag the
analytics layer already respects.

Per-source policy at v1 (set by migration 0003):

- ``steam_market``: 60 min interval, 5s per-item delay. 50 items per
  cycle (full watchlist while it fits in 50; rotation logic kicks in
  if the watchlist grows past 50).
- ``skinport``:     15 min interval, bulk fetch (per-item delay N/A,
  stored as 0). ``enabled`` is controlled through the ``sources`` row.
- ``dmarket``:      15 min interval, 3s per-item delay.

All jobs run under ``BlockingScheduler`` with ``max_instances=1`` and
``coalesce=True``: if a cycle is still running when its next interval
ticks, the next firing is skipped (and logged) rather than running
concurrently. See ADR 009 for the foundational design.

Conditional writes: every observation is compared against the most
recent row for the same ``(item, source)`` via
``collectors.base.should_write_observation`` before persistence.

Rate-limit handling (ADR 013):

- Collectors raise ``RateLimited`` on 429 retry exhaustion, carrying
  the most recent ``Retry-After`` header value (or None).
- The cycle wrapper catches it, computes a pause (header value, or a
  doubling 5min→1h fallback ladder), and reschedules that source's
  next firing via ``scheduler.modify_job``. Other sources keep running.
- Soft-degrade (Steam's ``success:true`` with no price for many items
  at once) is handled by a cycle-level heuristic in ``_run_cycle``:
  if more than ``AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD`` of a cycle's
  items came back empty, all ambiguous (None-yielded) outcomes are
  re-labeled ``declined`` rather than ``unavailable``. The split keeps
  rate-limit-disguise noise out of the "rare item with no listings"
  signal the bot will eventually surface.

Shutdown: SIGTERM and SIGINT trigger ``scheduler.shutdown(wait=True)``
— APScheduler drains any in-flight job, then ``start()`` returns and
the process exits 0. Docker's ``stop_grace_period: 5m`` allows a long
Steam cycle to drain.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from collectors.base import (
    DECLINED,
    Collector,
    PriceObservation,
    RateLimited,
    _DeclinedMarker,
    persist_observation,
    should_write_observation,
    update_observation_log,
)
from collectors.dmarket import DMarketCollector
from collectors.skinport import SkinportCollector
from collectors.steam import SteamCollector
from db.connection import get_engine
from db.models import Source
from db.naming import normalize_name
from scripts.seed_watchlist import DEFAULT_WATCHLIST_PATH, load_watchlist

logger = logging.getLogger(__name__)


# Steam: 50 items per cycle. Our watchlist is 48, so this is a ceiling
# not a slicing rule. If the watchlist ever grows past 50, add proper
# round-robin rotation (see ADR 009 §7).
STEAM_MAX_ITEMS_PER_CYCLE = 50

# Cycle-level heuristic: if more than this fraction of a cycle's items
# came back empty (DECLINED + ambiguous-None combined), treat the whole
# cycle as source-degraded and re-label the ambiguous ones as DECLINED.
# 0.5 chosen against the May 2026 Steam degradation event — baseline
# rare-items rate is ~20% (10/48), degraded cycles ran 56% (27/48) and
# 100% (48/48). A 50% threshold cleanly separates the two regimes.
# See ADR 013 §3.
AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD = 0.5

# Fallback pause ladder (used when 429 carried no Retry-After header):
# 5 min initial, double on repeat 429 within the same hour, cap 1 hour.
RATE_LIMIT_FALLBACK_INITIAL_SECONDS = 300  # 5 min
RATE_LIMIT_FALLBACK_CAP_SECONDS = 3600  # 1 hour
RATE_LIMIT_FALLBACK_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class SourceJobSpec:
    """Per-source registration data resolved from the ``sources`` table.

    Carried as a small dataclass so tests can inject a synthetic list
    without touching the DB.
    """

    name: str
    interval_minutes: int
    per_item_delay_seconds: int


@dataclass
class _RateLimitMemory:
    """Per-source rate-limit history used by the fallback ladder."""

    last_429_time: datetime
    current_pause_seconds: int


# Mutable module-level state for the running scheduler. Cycle wrappers
# read from these.
_rate_limit_state: dict[str, _RateLimitMemory] = {}
_state_lock = threading.Lock()
_active_source_specs: dict[str, SourceJobSpec] = {}
_scheduler_ref: BlockingScheduler | None = None

# DMarket aliases are loaded once per scheduler process from
# data/watchlist.yaml. Restart the collector service after alias edits.
_DMARKET_ALIAS_MAP: dict[str, frozenset[str]] = {}


# Source name → (collector class, log label, per-cycle watchlist limit).
# Adding a fourth source is a config change: insert row in `sources`,
# implement a Collector subclass, add an entry here, and (if it has its
# own job-wrapper callable) extend ``_SOURCE_CALLABLES``.
SOURCE_REGISTRY: dict[str, tuple[type[Collector], str, int | None]] = {
    "steam_market": (SteamCollector, "Steam", STEAM_MAX_ITEMS_PER_CYCLE),
    "skinport": (SkinportCollector, "Skinport", None),
    "dmarket": (DMarketCollector, "DMarket", None),
}


def _load_dmarket_alias_map(
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
) -> dict[str, frozenset[str]]:
    """Read watchlist.yaml, return ``{normalize_name(market_hash_name):
    frozenset of normalize_name(alias)}`` for curated-tier items with a
    non-empty ``dmarket_alias`` list.

    Featured-tier items with ``dmarket_alias`` are silently skipped
    here — the watchlist loader already logged a WARN at YAML-load
    time. Items without the field don't appear in the result.
    """
    data = load_watchlist(watchlist_path)
    alias_map: dict[str, frozenset[str]] = {}
    for item in data["items"]:
        if item.get("tier") != "curated":
            continue
        aliases = item.get("dmarket_alias")
        if not aliases:
            continue
        canonical = normalize_name(item["market_hash_name"])
        alias_map[canonical] = frozenset(
            normalize_name(a) for a in aliases
        )
    return alias_map


def _load_enabled_sources(session: Session) -> list[SourceJobSpec]:
    """Read the ``sources`` table for rows with ``enabled = TRUE``.

    Order by ``id`` so log lines and test assertions are deterministic.
    """
    rows = session.execute(
        select(
            Source.name,
            Source.interval_minutes,
            Source.per_item_delay_seconds,
        )
        .where(Source.enabled.is_(True))
        .order_by(Source.id)
    ).all()
    return [
        SourceJobSpec(
            name=row.name,
            interval_minutes=row.interval_minutes,
            per_item_delay_seconds=row.per_item_delay_seconds,
        )
        for row in rows
    ]


# Per-source tier filter:
#   steam_market, dmarket -> curated tier only
#   skinport              -> curated + featured
#   pricempire            -> bypasses this function entirely
_CURATED_ONLY_SOURCES: frozenset[str] = frozenset(
    {"steam_market", "dmarket"}
)


def _load_watchlist(
    session: Session,
    *,
    source_name: str,
    limit: int | None = None,
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
) -> list[str]:
    """Return the active watchlist for ``source_name``, filtered by
    tier per ADR 024.

    Reads from ``data/watchlist.yaml`` (NOT the items table). Items
    present in items but absent from YAML are silently excluded. The
    ``session`` parameter is retained for signature stability.

    Tier filtering:
    - source_name in {"steam_market", "dmarket"} → curated tier only
      (rate-limit math: 5s/item × 500 items > 60-min cycle for Steam;
      3s/item × 500 items > 15-min cycle for DMarket).
    - source_name == "skinport" → curated + featured (bulk-fetch
      endpoint; filtering happens in Python after a single HTTP call).
    - Pricempire bypasses this function entirely; its collect_snapshot
      reads the items table directly so substrate-row data continues
      to accumulate (ADR 024's data-preservation invariant).

    Loaded once per cycle by ``_run_cycle``. YAML edits require a
    collector restart to refresh the process-local alias map.
    """
    # session is reserved for future use (e.g. an alternative
    # tier-from-DB path); ignored under the current YAML-driven shape.
    del session
    data = load_watchlist(watchlist_path)
    if source_name in _CURATED_ONLY_SOURCES:
        names = [
            item["market_hash_name"]
            for item in data["items"]
            if item.get("tier") == "curated"
        ]
    else:
        names = [
            item["market_hash_name"]
            for item in data["items"]
            if item.get("tier") in {"curated", "featured"}
        ]
    names.sort()  # alphabetical, matches prior items-table ORDER BY
    if limit is not None and len(names) > limit:
        # Rotation backlog: naive slice always picks the first
        # `limit` items, starving the rest if watchlist grows past 50.
        # ADR 009 §7 has the design context for round-robin rotation.
        names = names[:limit]
    return names


def compute_pause_seconds(
    source_name: str,
    retry_after_seconds: int | None,
    now: datetime | None = None,
) -> int:
    """Decide pause duration for ``source_name`` after a 429-exhaustion.

    If the upstream sent a ``Retry-After`` header, use it directly —
    the server told us how long to wait. Otherwise use a doubling
    ladder: 5 min initial; if another 429 fires within 1 hour, double
    (5→10→20→…) capped at 1 hour. After 1 hour with no 429s, the ladder
    resets to 5 min.

    Updates module-level ``_rate_limit_state`` so subsequent calls see
    the new memory. Thread-safe.
    """
    if now is None:
        now = datetime.now(UTC)

    if retry_after_seconds is not None:
        with _state_lock:
            _rate_limit_state[source_name] = _RateLimitMemory(
                last_429_time=now,
                current_pause_seconds=retry_after_seconds,
            )
        return retry_after_seconds

    with _state_lock:
        memory = _rate_limit_state.get(source_name)
        if (
            memory is not None
            and (now - memory.last_429_time) < RATE_LIMIT_FALLBACK_WINDOW
        ):
            new_pause = min(
                memory.current_pause_seconds * 2,
                RATE_LIMIT_FALLBACK_CAP_SECONDS,
            )
        else:
            new_pause = RATE_LIMIT_FALLBACK_INITIAL_SECONDS
        _rate_limit_state[source_name] = _RateLimitMemory(
            last_429_time=now,
            current_pause_seconds=new_pause,
        )
        return new_pause


def _apply_pause(source_name: str, pause_seconds: int) -> None:
    """Defer ``source_name``'s next APScheduler firing by ``pause_seconds``.

    No-op (with INFO log) if the scheduler reference isn't set — that's
    the test path. APScheduler errors are caught and logged so a
    misconfigured job ID can't crash the cycle wrapper.
    """
    if _scheduler_ref is None:
        logger.info(
            "%s rate-limited: scheduler ref unset, skipping job pause "
            "(pause=%ds)",
            source_name,
            pause_seconds,
        )
        return
    job_id = f"{source_name}_cycle"
    next_run = datetime.now(UTC) + timedelta(seconds=pause_seconds)
    try:
        _scheduler_ref.modify_job(job_id, next_run_time=next_run)
        logger.warning(
            "%s job paused until %s (in %ds)",
            source_name,
            next_run.isoformat(),
            pause_seconds,
        )
    except Exception:
        logger.exception(
            "Failed to defer %s next run by %d seconds",
            source_name,
            pause_seconds,
        )


def _run_cycle(
    collector: Collector,
    source_label: str,
    watchlist_limit: int | None = None,
) -> None:
    """Run one cycle for ``collector`` over the current watchlist.

    Counters:

    - ``written``: persisted to ``prices``
    - ``unchanged``: matched the most-recent row exactly; dedup skipped
    - ``unavailable``: collector returned ambiguous-None (Steam
      ``success:false`` / Skinport ``min_price:null`` / DMarket empty
      ``objects[]``) AND the cycle did not exceed the degraded-cycle
      threshold
    - ``declined``: collector returned ``DECLINED`` (4xx non-429, retry
      exhaustion on timeouts/5xx, bulk fetch error), OR the cycle as a
      whole exceeded the threshold and an ambiguous-None was re-labeled
    - ``lookup_failed``: item or source name missing from DB (defensive;
      should be 0 in steady state)

    If the collector raises ``RateLimited`` mid-cycle, the partial
    results before the raise are still counted, then the source's job
    is paused for the computed duration.
    """
    engine = get_engine()

    with Session(engine) as session:
        watchlist: Iterable[str] = _load_watchlist(
            session,
            source_name=collector.source_name,
            limit=watchlist_limit,
        )

    watchlist = list(watchlist)
    if not watchlist:
        logger.warning("%s cycle: watchlist empty, skipping", source_label)
        return

    logger.info(
        "%s cycle starting: %d items", source_label, len(watchlist)
    )

    rate_limited_exc: RateLimited | None = None
    outcomes: list[PriceObservation | _DeclinedMarker | None] = []

    try:
        with collector.make_client() as client:
            for obs in collector.collect_cycle(client, watchlist):
                outcomes.append(obs)
    except RateLimited as exc:
        rate_limited_exc = exc
        logger.warning(
            "%s cycle aborted by RateLimited after %d items consumed",
            source_label,
            len(outcomes),
        )

    # Cycle-level heuristic: relabel ambiguous Nones as declined if a
    # large fraction of the cycle came back empty. Universal rule
    # (applies to all sources); for Skinport/DMarket where ambiguity
    # is rare in practice, the threshold simply won't be reached. For
    # Steam this is the soft-degrade detection. ADR 013 §3.
    none_count = sum(1 for o in outcomes if o is None)
    declined_explicit = sum(1 for o in outcomes if o is DECLINED)
    empty_fraction = (
        (none_count + declined_explicit) / len(outcomes)
        if outcomes
        else 0.0
    )
    relabel_none_as_declined = (
        empty_fraction > AMBIGUOUS_CYCLE_DEGRADED_THRESHOLD
        and none_count > 0
    )
    if relabel_none_as_declined:
        logger.warning(
            "%s cycle: %d/%d items came back empty (%.0f%%) — "
            "re-labeling %d ambiguous-Nones as declined "
            "(source-degraded heuristic)",
            source_label,
            none_count + declined_explicit,
            len(outcomes),
            empty_fraction * 100,
            none_count,
        )

    written = 0
    unchanged = 0
    unavailable = 0
    declined = 0
    lookup_failed = 0

    with Session(engine) as session:
        for obs in outcomes:
            if obs is DECLINED:
                declined += 1
                continue
            if obs is None:
                if relabel_none_as_declined:
                    declined += 1
                else:
                    unavailable += 1
                continue
            # PriceObservation — update observation_log unconditionally
            # (pre-dedup), so the streak counter sees a fresh "polled"
            # timestamp even when the price was unchanged. ADR 015.
            update_observation_log(session, obs)
            if not should_write_observation(session, obs):
                unchanged += 1
                continue
            if persist_observation(session, obs):
                # Commit per item so abrupt shutdown keeps partial
                # progress.
                session.commit()
                written += 1
            else:
                lookup_failed += 1
        session.commit()

    logger.info(
        "%s cycle complete: %d attempted, %d written, %d unchanged, "
        "%d unavailable, %d declined, %d lookup_failed",
        source_label,
        len(watchlist),
        written,
        unchanged,
        unavailable,
        declined,
        lookup_failed,
    )

    # If retry exhaustion happened, defer the next firing of this
    # source's job. Compute the pause AFTER the cycle summary so the
    # outcome is recorded even if modify_job fails.
    if rate_limited_exc is not None:
        pause = compute_pause_seconds(
            rate_limited_exc.source_name,
            rate_limited_exc.retry_after_seconds,
        )
        logger.warning(
            "%s rate-limited (Retry-After=%s) — pausing job for %ds",
            source_label,
            (
                rate_limited_exc.retry_after_seconds
                if rate_limited_exc.retry_after_seconds is not None
                else "absent"
            ),
            pause,
        )
        _apply_pause(rate_limited_exc.source_name, pause)


def _run_named_source(source_name: str) -> None:
    """Run one cycle for the source named ``source_name``.

    Reads the active SourceJobSpec from module state for the per-item
    delay; resolves the collector class from SOURCE_REGISTRY. Logs and
    swallows any unhandled exception so APScheduler doesn't lose the
    cycle summary line.
    """
    spec = _active_source_specs.get(source_name)
    if spec is None:
        logger.warning(
            "Source %r requested but not registered — skipping cycle",
            source_name,
        )
        return
    if source_name not in SOURCE_REGISTRY:
        logger.warning(
            "Source %r enabled in DB but no collector registered — "
            "skipping cycle",
            source_name,
        )
        return
    collector_cls, source_label, watchlist_limit = SOURCE_REGISTRY[source_name]
    try:
        if source_name == "dmarket":
            collector = collector_cls(alias_map=_DMARKET_ALIAS_MAP)
        else:
            collector = collector_cls()
        # Per-item delay from DB; instance-attr shadows the class
        # attribute used by Collector.collect_cycle.
        collector.inter_request_delay = float(spec.per_item_delay_seconds)
        _run_cycle(
            collector, source_label, watchlist_limit=watchlist_limit
        )
    except Exception:
        logger.exception(
            "%s cycle failed with unhandled exception", source_label
        )


def run_steam_cycle() -> None:
    """APScheduler job wrapper for Steam Market."""
    _run_named_source("steam_market")


def run_skinport_cycle() -> None:
    """APScheduler job wrapper for Skinport."""
    _run_named_source("skinport")


def run_dmarket_cycle() -> None:
    """APScheduler job wrapper for DMarket."""
    _run_named_source("dmarket")


def run_pricempire_cycle() -> None:
    """APScheduler job wrapper for the Pricempire bulk snapshot.

    Unlike per-item collectors, Pricempire's cycle is one HTTP call
    servicing all six sub-providers (ADR 018/019). The actual work
    lives in ``collectors.pricempire.collect_snapshot`` — that
    function already handles its own logging, dedup, and failure
    paths. This wrapper just guards against any unhandled exception
    so APScheduler doesn't drop the next cycle's summary line.
    """
    # Import lazily so a missing PRICEMPIRE_API_KEY at module-import
    # time doesn't break the rest of the scheduler.
    from collectors import pricempire

    try:
        pricempire.collect_snapshot()
    except Exception:
        logger.exception(
            "Pricempire cycle failed with unhandled exception"
        )


# Maps source name → the job callable APScheduler registers. Module-level
# so tests can locate them by string and so dynamic dispatch in
# _run_named_source stays out of APScheduler's job-pickling path.
_SOURCE_CALLABLES: dict[str, object] = {
    "steam_market": run_steam_cycle,
    "skinport": run_skinport_cycle,
    "dmarket": run_dmarket_cycle,
    "pricempire": run_pricempire_cycle,
}


# Pseudo-sources are scheduled like real sources but don't go through
# the per-item Collector / _run_named_source path. They live directly
# in _SOURCE_CALLABLES and bypass SOURCE_REGISTRY entirely. The
# pricempire bulk-snapshot is the only one today (ADR 018 §3); future
# bulk-import sources would join this set.
_PSEUDO_SOURCES: frozenset[str] = frozenset({"pricempire"})


def build_scheduler(
    source_jobs: list[SourceJobSpec] | None = None,
) -> BlockingScheduler:
    """Build (but don't start) the scheduler.

    Iterates ``sources WHERE enabled = TRUE`` and registers one job per
    enabled source, reading interval and per-item delay from the same
    row. Disabled sources are not scheduled — the DB flag is the single
    switch for "do not poll this source."

    For testability, callers can inject ``source_jobs`` to skip the DB
    read. Production leaves it None and the DB is queried.
    """
    if source_jobs is None:
        with Session(get_engine()) as session:
            source_jobs = _load_enabled_sources(session)

    # Reset active-specs cache. Module-level dict — a subsequent
    # build_scheduler in the same process (e.g. tests) gets a clean view.
    _active_source_specs.clear()

    # Alias-map loading is fail-fast because collectors require a
    # valid watchlist.
    global _DMARKET_ALIAS_MAP
    _DMARKET_ALIAS_MAP = _load_dmarket_alias_map()
    if _DMARKET_ALIAS_MAP:
        logger.info(
            "Loaded DMarket alias map: %d items with aliases",
            len(_DMARKET_ALIAS_MAP),
        )

    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": 300,
        },
    )

    # ``next_run_time`` fires each job a second after the scheduler
    # starts (without it, the first run would wait a full interval).
    # Also avoids running cycles BEFORE ``scheduler.start()``, which
    # leaves the SIGTERM handler unable to call ``scheduler.shutdown()``
    # cleanly (raises SchedulerNotRunningError).
    soon = datetime.now(UTC) + timedelta(seconds=1)

    for spec in source_jobs:
        # Skip the six pricempire sub-provider rows — they're not
        # independently scheduled (ADR 018/019). One Pricempire HTTP
        # call services them all under the `pricempire` pseudo-source.
        if spec.name.startswith("pricempire_"):
            continue

        if spec.name in _PSEUDO_SOURCES:
            # Pseudo-sources bypass SOURCE_REGISTRY — they don't have
            # a per-item Collector class. The callable is wired
            # directly in _SOURCE_CALLABLES.
            callable_ = _SOURCE_CALLABLES.get(spec.name)
            if callable_ is None:
                logger.error(
                    "Pseudo-source %r has no callable — skipping "
                    "(this is a programming error)",
                    spec.name,
                )
                continue
            scheduler.add_job(
                callable_,
                trigger="interval",
                minutes=spec.interval_minutes,
                next_run_time=soon,
                id=f"{spec.name}_cycle",
                name=(
                    f"{spec.name} pseudo-cycle "
                    f"(every {spec.interval_minutes}m)"
                ),
            )
            logger.info(
                "Registered %s pseudo-cycle: interval=%dm",
                spec.name,
                spec.interval_minutes,
            )
            continue

        if spec.name not in SOURCE_REGISTRY:
            logger.warning(
                "Source %r enabled in DB but no collector registered — "
                "skipping",
                spec.name,
            )
            continue
        callable_ = _SOURCE_CALLABLES.get(spec.name)
        if callable_ is None:
            logger.error(
                "Source %r in SOURCE_REGISTRY but no callable — "
                "skipping (this is a programming error)",
                spec.name,
            )
            continue
        _active_source_specs[spec.name] = spec
        _, source_label, _ = SOURCE_REGISTRY[spec.name]
        scheduler.add_job(
            callable_,
            trigger="interval",
            minutes=spec.interval_minutes,
            next_run_time=soon,
            id=f"{spec.name}_cycle",
            name=f"{source_label} poll (every {spec.interval_minutes}m)",
        )
        logger.info(
            "Registered %s cycle: interval=%dm, per_item_delay=%ds",
            source_label,
            spec.interval_minutes,
            spec.per_item_delay_seconds,
        )

    return scheduler


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    global _scheduler_ref

    logging.basicConfig(
        level=logging.INFO,
        format=(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"name":"%(name)s","msg":%(message)r}'
        ),
    )

    scheduler = build_scheduler()
    _scheduler_ref = scheduler

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "Signal %d received; finishing in-flight cycles and "
            "shutting down",
            signum,
        )
        try:
            scheduler.shutdown(wait=True)
        except SchedulerNotRunningError:
            logger.info(
                "Scheduler not running yet; exiting without start()"
            )
            sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    job_ids = [job.id for job in scheduler.get_jobs()]
    logger.info(
        "Scheduler entering main loop: %d jobs registered: %s "
        "(first run of each fires within ~1s)",
        len(job_ids),
        ", ".join(job_ids) if job_ids else "(none — no enabled sources)",
    )
    scheduler.start()  # blocks until shutdown() is called
    logger.info("Scheduler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
