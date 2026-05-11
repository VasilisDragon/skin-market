# ADR 002 — TimescaleDB over vanilla PostgreSQL

**Status:** Accepted
**Date:** 2026-05-11

## Context

The project's primary data shape is time-series: every collection cycle writes
one row per (item, source, timestamp). At steady state we expect roughly
50 items × 2+ sources × 48 cycles/day = ~5,000 rows/day per source-tier,
growing as the watchlist and source count grow. Queries are almost always
range scans by time (last 7 days, last 30 days, etc.) for a given item.

We need efficient time-bucket aggregations (e.g. daily OHLC summaries), the
ability to retain raw data for at least a year, and a path to compression as
the dataset grows beyond what a hot working set can fit in RAM.

## Decision

Use PostgreSQL with the TimescaleDB extension. The `prices` table is created
as a hypertable partitioned on `timestamp`. The `items`, `sources`, and
`insights` tables remain regular Postgres tables.

The chosen Docker image is `timescale/timescaledb:2.17.2-pg16` — pinned to a
specific patch version, never `:latest`.

## Alternatives considered

- **Vanilla Postgres + manual partitioning (`pg_partman` or hand-rolled)**:
  works, but reinvents what TimescaleDB does natively — chunk pruning,
  continuous aggregates, compression policies. Rejected on engineering-cost
  grounds.
- **ClickHouse**: excellent for OLAP-style time-series and faster than
  TimescaleDB at scale, but introduces a second database (we'd still want
  Postgres for the relational `items`/`sources` metadata), and the team has
  no operational experience with it.
- **InfluxDB**: a different paradigm (Flux/InfluxQL), poor fit for our
  relational joins between time-series and metadata tables.
- **DuckDB**: great for analytics but not designed for concurrent
  write workloads from multiple collector processes.

## Consequences

- **Pro:** native time-bucket aggregations (`time_bucket()`), continuous
  aggregates for pre-computed rollups, compression policies for cold data,
  all expressed in standard SQL.
- **Pro:** still PostgreSQL — same tools (`psql`, `pg_dump`), same
  SQLAlchemy driver, same backups.
- **Con:** locked into the TimescaleDB extension. Migrating off would mean
  rewriting hypertable definitions as native partitioning. Acceptable
  trade-off for the ergonomic gains.
- **Con:** the Docker image is ~150 MB larger than vanilla Postgres.
  Acceptable for a single-host deployment.
- **Con:** managed-hosting choices are narrower (Timescale Cloud, or
  self-hosted). Acceptable — v1 deploys to the Spark.
- ADR 003 (FastAPI) is independent of this choice.
