# Known limitations and deferred work

- Per-source tier-filtering currently lives in multiple modules
  (`collectors/scheduler.py`, `analytics/drift.py`,
  `analytics/anomaly_detection.py`, `api/watchlist_tiers.py`).
  Consolidation tracked for a future ADR.
- Synchronous I/O patterns in some bot tools and analytics jobs. The bot
  is single-event-loop today; analytics jobs are batch-mode under
  APScheduler. Will revisit when concurrency demands it.
- Query-cost logging for abuse-potential analysis is deferred until
  public or multi-tenant access exists.
- A Redis caching layer is deferred until a measurable latency or load
  problem appears.
