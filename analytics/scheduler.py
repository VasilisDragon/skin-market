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
        "Analytics scheduler entering main loop: hourly cycle + 02:00 UTC narrative"
    )
    scheduler.start()
    logger.info("Analytics scheduler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
