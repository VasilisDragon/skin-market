# ADR 004 — psycopg 3 over psycopg2-binary

**Status:** Accepted
**Date:** 2026-05-11
**Supersedes part of:** ADR 001's library shortlist (ARCHITECTURE.md and ADR 001 updated accordingly)

## Context

ARCHITECTURE.md's initial "boring tools" shortlist named `psycopg2-binary` as the
Postgres driver. After Phase 0 review, the user pointed out two problems:

1. **psycopg2-binary is not production-suitable per psycopg's own docs.** The
   `-binary` distribution statically links `libpq` and OpenSSL into the
   wheel. When a distro patches an OpenSSL CVE, the linked copy inside the
   psycopg2-binary wheel is unaffected — the only fix is a new wheel
   release. For a service that may sit on a host for months between
   rebuilds, this is a meaningful exposure.
2. **psycopg 3 is where the ecosystem is going.** It's async-native,
   provides better Postgres feature coverage (COPY, listen/notify,
   prepared-statement caching, named cursors, server-side binding),
   and is supported by SQLAlchemy 2.x out of the box.

## Decision

Use `psycopg[binary]>=3.2,<4.0` as the Postgres driver. The `[binary]`
extra still ships a precompiled wheel for development convenience, but
psycopg 3's distribution model (separate `psycopg`, `psycopg-binary`,
`psycopg-c` packages) makes it straightforward to swap to a
distro-libpq-linked install in production by installing plain `psycopg`
on a host that has `libpq` from the system package manager.

The SQLAlchemy URL scheme is `postgresql+psycopg://` (the psycopg-2
scheme `postgresql+psycopg2://` is unrelated and rejected by SQLAlchemy
when only psycopg 3 is installed).

## Alternatives considered

- **psycopg2 (non-binary)**: dynamically links libpq, so distro CVE
  patches flow through — but requires `libpq-dev` on every build host,
  and is the older driver with fewer Postgres features. Rejected because
  psycopg 3 supersedes it.
- **asyncpg**: faster than psycopg for async workloads, but not the
  default async driver for SQLAlchemy 2.x (psycopg 3 is), uses a
  bespoke type-coercion model, and would force separate sync vs async
  paths for the collectors (sync) and the FastAPI app (async). Rejected
  to keep one driver.
- **pg8000 / pure-python drivers**: slow and feature-limited. Rejected.

## Consequences

- **Pro:** ecosystem-aligned driver, async-ready when the API needs it,
  better security posture in production (swap to non-binary install with
  distro libpq).
- **Pro:** psycopg 3 has nicer `COPY` ergonomics, which the analytics
  layer may use for bulk insert later.
- **Con:** smaller volume of legacy Stack Overflow answers; the user
  must know to search for "psycopg 3" specifically. Mitigation:
  documented here.
- **Con:** the `[binary]` extra is still statically linked. For
  production deployment to the Spark, when the time comes, install
  plain `psycopg` against a system `libpq`. Track this as a v1→v2
  hardening item.
- **Updates:** ARCHITECTURE.md's "boring tools" list and ADR 001's example
  list now reference `psycopg` instead of `psycopg2-binary`.
