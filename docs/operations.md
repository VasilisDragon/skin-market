# Operations

Operational recipes for skin-market — what to type when you're paged at
2am, where to look for status, what NOT to touch. Most of this is also
documented in scattered comments, ADRs, and the README; this file is
the consolidated cheat sheet.

## Starting and stopping the collector

The collector is the long-running service that polls Steam and Skinport
on schedule. Postgres is its only dependency.

```bash
# Start (postgres comes up first via depends_on healthcheck)
docker compose up -d collector

# Status
docker compose ps

# Stop (graceful: drains in-flight cycles; up to ~5 min for a Steam cycle)
docker compose stop collector

# Restart (preserves all collected data; postgres volume untouched)
docker compose restart collector

# Hard kill (last resort; per-item commits mean mid-cycle progress survives)
docker kill skinmarket-collector
```

The `stop_grace_period` in `docker-compose.yml` is 5 minutes. A
`docker compose stop` or `docker compose down` during a running Steam
cycle waits up to that long before SIGKILL. Operators who don't know
this will think Docker hung — it hasn't.

## Reading cycle logs

Logs are structured JSON on stdout. The cycle-summary lines are the
operational signal; everything else is noise during normal operation.

```bash
# Tail in real time
docker compose logs -f --tail 30 collector

# Operational summary: cycle outcomes only (the one-liner worth bookmarking)
docker compose logs collector | grep "cycle complete"

# Recent errors only
docker compose logs --since 1h collector | grep -E "ERROR|Traceback"

# What's happening RIGHT NOW
docker compose logs --since 30s collector | tail -20
```

A healthy cycle summary looks like:

```
Skinport cycle complete: 48 attempted, 3 written, 44 unchanged,
    1 unavailable, 0 lookup_failed
```

The four outcome counters are mutually exclusive and sum to `attempted`:

| Counter | Meaning |
|---|---|
| `written` | Row landed in `prices`. Real signal. |
| `unchanged` | Dedup skipped — same (price, volume) as the previous row for this (item, source). High `unchanged` is normal during quiet market periods. |
| `unavailable` | Collector returned None — upstream said "no listings" (Steam `success:false` / Skinport `min_price:null`) or retry-exhausted. |
| `lookup_failed` | Item or source not in DB. Should be 0 in steady state — non-zero means the seed didn't run or the watchlist YAML drifted. |

If you see `lookup_failed > 0`:

```bash
# Did the seed run? Items count should be 48 (or whatever the YAML has)
docker exec skinmarket-postgres psql -U skinmarket -d skinmarket \
    -c "SELECT COUNT(*) FROM items; SELECT COUNT(*) FROM sources;"

# Re-seed if needed (idempotent ON CONFLICT DO NOTHING; safe to re-run)
uv run python -m scripts.seed_watchlist
```

## Tests

Default `uv run pytest` is safe to run against any DB state. It skips
destructive tests via `addopts = "-ra -m 'not destructive'"` in
`pyproject.toml`.

```bash
# Default — never wipes data. Run before commits.
uv run pytest

# Destructive — drops + recreates all domain tables, re-seeds.
# WIPES collected price/insight data. Only run on a throwaway dev DB.
uv run pytest -m destructive
```

The single destructive test is `test_migration_roundtrip_then_seed`, which
exists to prove migrations round-trip cleanly. It re-seeds items and
sources at the end, so the watchlist is restored — but `prices` and
`insights` are empty afterward.

## Postgres password rotation footgun

`POSTGRES_PASSWORD` in `.env` is only consumed by the postgres container
on the **first init of the data volume**. After that:

- Changing `.env`'s password does NOT update the running postgres.
- The postgres container will keep accepting the OLD password.
- The collector container reads the NEW password from compose at
  startup and will fail to authenticate.

Symptom: collector logs show `psycopg.OperationalError: password
authentication failed for user "skinmarket"`. Postgres logs show
"FATAL: password authentication failed for user."

Recovery — pick one:

**Option 1 (recommended): ALTER USER inside the running postgres.**

```bash
# Set the new password to match what's now in .env
docker exec -it skinmarket-postgres psql -U skinmarket -d skinmarket \
    -c "ALTER USER skinmarket WITH PASSWORD 'new_password_from_env'"

# Restart the collector so it reconnects
docker compose restart collector
```

**Option 2 (destructive): drop the data volume.**

```bash
docker compose down -v   # WIPES all collected price + insight data
docker compose up -d     # re-inits postgres from .env's new password
```

Option 1 is almost always what you want. Option 2 only makes sense if
you also intended to start fresh.

The header comment block in `docker-compose.yml` says the same thing in
two sentences; this is the longer version for when you're already
debugging.

## Related documentation

- `README.md` — Getting started, development setup, deployment summary
- `PROJECT_SPEC.md` — What each phase builds + acceptance criteria
- `ARCHITECTURE.md` — Workflow rules, libraries-of-choice, anti-patterns
- `docs/adr/*.md` — Architecture decisions, one file per non-obvious
  choice. The ones most relevant to operations:
  - 002 (TimescaleDB) — compression policy at 30 days
  - 006 (Collector resilience) — what each HTTP outcome does
  - 009 (Scheduler) — overlap policy, signal handling, conditional writes
