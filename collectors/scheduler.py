"""Scheduler service for the v1 collection layer.

Runs as the foreground process of the ``collector`` Docker service. Two
scheduled jobs:

- **Steam** — every 30 minutes; up to 50 items per cycle (full watchlist
  while it fits in 50; rotation logic will kick in if the watchlist grows
  past 50). Per-item 5-second pacing is enforced by
  ``Collector.inter_request_delay``.
- **Skinport** — every 5 minutes; one bulk fetch covering the full
  watchlist.

Both jobs run under ``BlockingScheduler`` with ``max_instances=1`` and
``coalesce=True``: if a cycle is still running when its next interval
ticks, the next firing is skipped (and logged) rather than running
concurrently. See ADR 009 for the full design rationale.

Conditional writes: every observation is compared against the most
recent row for the same ``(item, source)`` via
``collectors.base.should_write_observation`` before persistence. Exact
equality on ``(price, volume)`` — unchanged readings are silently
skipped. This is the main reason Skinport's ~13.5k-rows/day projection
will collapse to far less in practice for low-volatility items.

Shutdown: SIGTERM and SIGINT both trigger ``scheduler.shutdown(wait=True)``.
APScheduler finishes any in-flight job, then ``start()`` returns and the
process exits 0. Docker's ``stop_grace_period: 5m`` allows a long Steam
cycle to drain.
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from types import FrameType

from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from collectors.base import (
    Collector,
    persist_observation,
    should_write_observation,
)
from collectors.skinport import SkinportCollector
from collectors.steam import SteamCollector
from db.connection import get_engine
from db.models import Item

logger = logging.getLogger(__name__)


# Steam: 50 items per cycle. Our watchlist is 48, so this is a ceiling
# not a slicing rule. If the watchlist ever grows past 50, add proper
# round-robin rotation here (track an offset between cycles in a small
# state row, or just rotate based on `cycle_count % chunk_count`).
STEAM_MAX_ITEMS_PER_CYCLE = 50


def _load_watchlist(session: Session, limit: int | None = None) -> list[str]:
    rows = session.execute(
        select(Item.market_hash_name).order_by(Item.market_hash_name)
    ).all()
    names = [row[0] for row in rows]
    if limit is not None and len(names) > limit:
        # TODO(watchlist-rotation): when watchlist grows past 50, this
        # naive `names[:limit]` always picks the same first 50 items,
        # starving the rest. Replace with round-robin: store a
        # ``last_cycle_offset`` row in a small ``scheduler_state`` table
        # (or compute the offset from a wall-clock-derived index, e.g.
        # ``int(time.time() // interval_seconds) % chunk_count``), and
        # slice ``names[offset:offset+limit]`` with wraparound. Not
        # implemented because v1 watchlist is 48 < 50; ADR 009 §7 has
        # the design context.
        names = names[:limit]
    return names


def _run_cycle(
    collector: Collector,
    source_label: str,
    watchlist_limit: int | None = None,
) -> None:
    """Run one cycle for ``collector`` over the current watchlist.

    Logs a single summary line at the end. Counters split into:
    - written: persisted to ``prices``
    - unchanged: matched the most-recent row exactly; dedup skipped
    - unavailable: collector returned None (Steam success:false /
      Skinport min_price:null / retry exhaustion)
    - lookup_failed: item or source name missing from DB (should never
      happen in practice; defensive)
    """
    engine = get_engine()

    with Session(engine) as session:
        watchlist: Iterable[str] = _load_watchlist(
            session, limit=watchlist_limit
        )

    watchlist = list(watchlist)
    if not watchlist:
        logger.warning("%s cycle: watchlist empty, skipping", source_label)
        return

    logger.info(
        "%s cycle starting: %d items", source_label, len(watchlist)
    )

    with collector.make_client() as client:
        observations = list(collector.collect_cycle(client, watchlist))

    written = 0
    unchanged = 0
    unavailable = 0
    lookup_failed = 0

    with Session(engine) as session:
        for obs in observations:
            if obs is None:
                unavailable += 1
                continue
            if not should_write_observation(session, obs):
                unchanged += 1
                continue
            if persist_observation(session, obs):
                # Commit per-item so a mid-cycle SIGKILL keeps partial
                # progress, and so an operator can watch the prices
                # table grow live with `\watch` in psql.
                session.commit()
                written += 1
            else:
                lookup_failed += 1
        # Defensive final commit — in case anything snuck in without
        # going through persist_observation.
        session.commit()

    logger.info(
        "%s cycle complete: %d attempted, %d written, %d unchanged, "
        "%d unavailable, %d lookup_failed",
        source_label,
        len(watchlist),
        written,
        unchanged,
        unavailable,
        lookup_failed,
    )


def run_steam_cycle() -> None:
    """APScheduler job wrapper: never let a bad cycle take down the scheduler."""
    try:
        _run_cycle(
            SteamCollector(),
            "Steam",
            watchlist_limit=STEAM_MAX_ITEMS_PER_CYCLE,
        )
    except Exception:
        logger.exception("Steam cycle failed with unhandled exception")


def run_skinport_cycle() -> None:
    try:
        _run_cycle(SkinportCollector(), "Skinport")
    except Exception:
        logger.exception("Skinport cycle failed with unhandled exception")


def build_scheduler() -> BlockingScheduler:
    """Build (but don't start) the scheduler. Factored out for testability."""
    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={
            # One concurrent run per job. If a cycle overruns, the next
            # firing is dropped (with a "missed" log line from APScheduler)
            # rather than running in parallel and double-hitting upstream.
            "max_instances": 1,
            # If multiple firings stack up (e.g. process slept), collapse
            # them into one.
            "coalesce": True,
            # Tolerate small clock skew / GC pauses.
            "misfire_grace_time": 300,
        },
    )

    # ``next_run_time`` fires each job a second after the scheduler starts
    # (without it, the first run would wait a full interval — 30 min for
    # Steam, 5 min for Skinport). This also avoids running cycles BEFORE
    # ``scheduler.start()``, which would leave the SIGTERM handler unable
    # to use ``scheduler.shutdown()`` (raises SchedulerNotRunningError).
    soon = datetime.now(UTC) + timedelta(seconds=1)
    scheduler.add_job(
        run_steam_cycle,
        trigger="interval",
        minutes=30,
        next_run_time=soon,
        id="steam_cycle",
        name="Steam Market priceoverview poll",
    )
    scheduler.add_job(
        run_skinport_cycle,
        trigger="interval",
        minutes=5,
        next_run_time=soon,
        id="skinport_cycle",
        name="Skinport bulk poll",
    )
    return scheduler


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 — argv for parity
    logging.basicConfig(
        level=logging.INFO,
        format=(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"name":"%(name)s","msg":%(message)r}'
        ),
    )

    scheduler = build_scheduler()

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "Signal %d received; finishing in-flight cycles and shutting down",
            signum,
        )
        # wait=True blocks until every running job finishes — that's why
        # docker-compose.yml uses stop_grace_period: 5m. The scheduler
        # then returns control to start() below, which returns, and we
        # exit cleanly with status 0.
        try:
            scheduler.shutdown(wait=True)
        except SchedulerNotRunningError:
            # Race window between signal.signal() and scheduler.start().
            # The scheduler hasn't started, so there's nothing to drain;
            # exit immediately so the container doesn't end up running
            # forever oblivious to the SIGTERM.
            logger.info(
                "Scheduler not running yet; exiting without start()"
            )
            sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info(
        "Scheduler entering main loop: Steam every 30m, Skinport every 5m "
        "(first run of each fires within ~1s)"
    )
    scheduler.start()  # blocks until shutdown() is called
    logger.info("Scheduler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
