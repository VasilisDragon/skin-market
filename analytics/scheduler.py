"""Analytics scheduler service.

Runs as the foreground process of the ``analytics`` Docker service.
Two scheduled jobs:

- **hourly** — at minute 5 of every hour. Computes moving averages,
  cross-source view/spread, and anomalies (volume + cross-source
  divergence). All four go through their own module under
  ``analytics/``; this scheduler is just orchestration.
- **daily narrative** — at 02:00 UTC. Pulls the day's notable moves,
  calls Ollama for a one-paragraph English summary, writes a
  ``daily_narrative`` insights row.

Design choices documented in ADR 010 (analytics architecture) and ADR
011 (narrative job).

Same shape as ``collectors/scheduler.py``: ``BlockingScheduler``,
per-job try/except, SIGTERM handler that survives the
``SchedulerNotRunningError`` race during boot, ``next_run_time=now+1s``
so the first cycle fires immediately on deploy without waiting an
hour.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from types import FrameType

from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from analytics import (
    anomaly_detection,
    cross_source,
    drift,
    moving_averages,
    narrative,
    unavailability_streak,
)
from db.connection import get_engine

logger = logging.getLogger(__name__)


def _run_with_logging(label: str, fn, *args, **kwargs) -> None:
    """Run a job step, log its outcome line, swallow exceptions."""
    try:
        result = fn(*args, **kwargs)
        logger.info("%s: wrote %s rows", label, result)
    except Exception:
        logger.exception("%s failed with unhandled exception", label)


def run_hourly_cycle() -> None:
    """The hourly analytics pass. Each sub-job is independently wrapped
    so one bad SQL run doesn't take the cycle down — partial success
    is better than no insights at all."""
    logger.info("Hourly analytics cycle starting")
    engine = get_engine()
    now = datetime.now(UTC)
    with Session(engine) as session:
        _run_with_logging(
            "Moving averages", moving_averages.compute_and_store, session, now
        )
        _run_with_logging(
            "Cross-source view + spread",
            cross_source.compute_and_store,
            session,
            now,
        )
        _run_with_logging(
            "Anomaly detection (volume + divergence)",
            anomaly_detection.compute_and_store,
            session,
            now,
        )
        _run_with_logging(
            "Unavailability streaks",
            unavailability_streak.compute_and_store,
            session,
            now,
        )
        session.commit()
    logger.info("Hourly analytics cycle complete")


def run_drift_cycle() -> None:
    """30-minute pattern-aware drift detection cycle (Phase 2b, ADR 022).

    Compares each deep-tier item's direct-collector latest price
    against the corresponding Pricempire sub-provider latest price.
    Emits one drift_verdict insights row per (item, meaningful-pair).
    The classifier (ADR 021) decides phase_based skips and pattern-
    seed elevated thresholds.

    Off-cycle from the hourly cycle to avoid queueing behind MA /
    cross-source / anomaly jobs; 30 min matches the stale-threshold
    so each cycle is guaranteed to see at least one fresh poll on
    each side under normal operation.

    ── Feature flag (Phase 2b Step 5) ────────────────────────────────
    Gated behind the ``DRIFT_DETECTION_ENABLED`` env var so the
    detector is dormant until Step 7's re-seed gate validates the
    deep-tier composition. Defaults to false at every analytics
    service restart — Step 7 flips it true after sign-off.

    Accepted truthy values (case-insensitive): "true", "1", "yes",
    "on". Anything else (unset, empty, "false", "0", etc.) means
    "the cycle fires but no-ops with a single log line."

    ── Runtime reload semantics ──────────────────────────────────────
    The classifier YAML (data/pattern_sensitivity.yaml) and the
    watchlist tier filter (data/watchlist.yaml) are loaded ONCE per
    cycle at the start of ``compute_and_store``. Across cycles, the
    Python process re-reads both files — but the analytics service's
    APScheduler keeps the same Python process alive between cycles,
    so an operator-side YAML edit takes effect on the NEXT 30-min
    tick after the file is saved. An analytics service restart is
    NOT required for YAML edits to apply, but a restart IS required
    if you change module-level constants (BASELINE_DRIFT_THRESHOLD,
    STALE_*_MINUTES, etc.) — those are bound at import time.
    """
    if not _drift_detection_enabled():
        logger.info(
            "Drift detection cycle: DRIFT_DETECTION_ENABLED not set "
            "(or falsy); cycle is a no-op. Set the env var to a "
            "truthy value to enable. Phase 2b Step 7 flips this."
        )
        return

    logger.info("Drift detection cycle starting")
    engine = get_engine()
    try:
        with Session(engine) as session:
            wrote = drift.compute_and_store(
                session, now=datetime.now(UTC)
            )
            session.commit()
        logger.info(
            "Drift detection cycle complete: wrote %d rows", wrote
        )
    except Exception:
        logger.exception(
            "Drift detection cycle failed with unhandled exception"
        )


def _drift_detection_enabled() -> bool:
    """Read ``DRIFT_DETECTION_ENABLED`` from the environment. Returns
    True when the env var is set to one of {"true", "1", "yes", "on"}
    (case-insensitive). Anything else — including unset — is False.

    Phase 2b Step 5 (feature flag): the drift detector is dormant
    until Step 7's re-seed gate validates the deep-tier composition.
    """
    raw = (os.environ.get("DRIFT_DETECTION_ENABLED") or "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def run_daily_narrative() -> None:
    """Nightly narrative job. Calls Ollama; stores one ``daily_narrative``
    insights row. Failure (Ollama down, empty response) logs ERROR and
    inserts nothing; the bot's reply path falls back gracefully."""
    logger.info("Daily narrative job starting")
    engine = get_engine()
    try:
        with Session(engine) as session:
            wrote = narrative.generate_and_store(session)
            if wrote:
                logger.info("Daily narrative: stored 1 row")
            else:
                logger.info("Daily narrative: nothing stored this run")
    except Exception:
        logger.exception("Daily narrative job failed with unhandled exception")


def build_scheduler() -> BlockingScheduler:
    """Build (but don't start) the scheduler. Same defaults as the
    collector's scheduler for consistency."""
    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": 300,
        },
    )

    soon = datetime.now(UTC) + timedelta(seconds=1)
    scheduler.add_job(
        run_hourly_cycle,
        trigger=IntervalTrigger(hours=1),
        next_run_time=soon,
        id="hourly_analytics",
        name="Hourly analytics (MAs + cross-source + anomalies)",
    )
    # Phase 2b — ADR 022. Off-cycle from hourly so it doesn't queue
    # behind MA/anomaly computations. 30 min matches the stale
    # threshold; each cycle sees at least one fresh poll on each
    # side under normal operation.
    scheduler.add_job(
        run_drift_cycle,
        trigger=IntervalTrigger(minutes=30),
        next_run_time=soon,
        id="drift_detection",
        name="Pattern-aware drift detection (30 min)",
    )
    scheduler.add_job(
        run_daily_narrative,
        # 02:00 UTC daily. Don't fire on boot — the hourly cycle runs
        # immediately, but the narrative wants accumulated data and
        # the LLM call is slow; let it wait for its proper slot.
        trigger=CronTrigger(hour=2, minute=0, timezone="UTC"),
        id="daily_narrative",
        name="Daily narrative (Ollama-driven)",
    )
    return scheduler


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
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
            "Signal %d received; finishing in-flight jobs and shutting down",
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

    logger.info(
        "Analytics scheduler entering main loop: hourly cycle + "
        "30-min drift detection + 02:00 UTC narrative"
    )
    scheduler.start()
    logger.info("Analytics scheduler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
