# Operations

Runbooks for skin-market — what to type when something is wrong, where to look for status, what NOT to touch. Most of this is also documented in scattered comments, ADRs, and the README; this file is the consolidated cheat sheet.

## Image-rebuild discipline (CRITICAL — this has bitten us 3+ times)

**`docker compose restart <service>` reuses the previous image. It does NOT pick up code changes.**

After any code change in `collectors/`, `analytics/`, `api/`, or `bot/`, the corresponding container needs:

```bash
docker compose build <service>
docker compose up -d <service>
```

Concrete symptom that you forgot to rebuild: logs show pre-fix behavior despite a committed fix. The classic ones (each a real fire-drill that taught us this rule):

- **Phase 5 stale-image fire-drill (commit `8e141d0`)** — `RateLimited` exception added but the running container kept retrying past exhaustion because the image was 18h old. Fixed by `docker compose build collector`.
- **Phase 6.5 DMarket title-fix** — strict title check added but the next collector cycle re-added the polluted rows. Rebuilt and restarted before the cleanup query ran.
- **Phase 7c-fix tool-result size discipline** — same shape; size summarizers landed in code but the container kept timing out on 48-item watchlist queries. Rebuild fixed.

**Diagnostic:**

```bash
docker images | grep skin-market-
# Look at the CREATED column. If your fix was committed in the last
# hour and the image is from 18 hours ago, you need to rebuild.
```

**Idiom we land on:** rebuild as part of any commit that changes the corresponding service's code, before considering the change "verified." If it's worth a commit, it's worth a rebuild.

## Service bring-up + health check

```bash
docker compose up -d            # all services
docker compose ps               # all should be Up; api Up (healthy)

# Smoke checks. Reuse the API token from .env.
TOKEN=$(grep ^SKIN_MARKET_API_TOKEN= .env | cut -d= -f2)

curl -sS http://localhost:8001/health
# {"status":"ok","db":"reachable"}

curl -sS -H "Authorization: Bearer $TOKEN" http://localhost:8001/items | jq 'length'
# 48

docker compose logs --tail 50 collector | grep "cycle complete"
# At least one healthy cycle from each enabled source in the last 60 minutes.

docker compose logs --tail 50 analytics | grep "Hourly analytics cycle"
# At least one in the last hour.

docker compose logs --tail 20 bot
# "Bot connected as <name> (id=…); allowlist size=N"
# If allowlist size is 0, DISCORD_ALLOWED_USER_IDS is empty.
```

## Reading cycle logs

Logs are structured JSON on stdout. Cycle-summary lines are the operational signal; everything else is noise during normal operation.

```bash
# Tail in real time
docker compose logs -f --tail 30 collector

# Operational summary — cycle outcomes only
docker compose logs collector | grep "cycle complete"

# Recent errors
docker compose logs --since 1h collector | grep -E "ERROR|Traceback"

# What's happening RIGHT NOW
docker compose logs --since 30s collector | tail -20
```

Healthy cycle summary shape:

```
Skinport cycle complete: 48 attempted, 3 written, 44 unchanged,
    1 unavailable, 0 declined, 0 lookup_failed
```

Six counters, mutually exclusive, sum to `attempted`:

| Counter | Meaning |
|---|---|
| `written` | Row landed in `prices`. Real signal. |
| `unchanged` | Dedup skipped — same (price, volume) as the previous row for this (item, source). High during quiet markets. |
| `unavailable` | Source confirmed no listings (Steam `success:false`, Skinport `min_price:null`, DMarket empty `objects[]`) AND the cycle didn't trip the degraded-cycle threshold. **Genuine "rare item, no current listings" signal.** |
| `declined` | Source declined to answer (4xx non-429, retry exhaustion on timeouts/5xx, bulk fetch error), OR the cycle exceeded the 50% degraded threshold and an ambiguous-`None` was re-labeled. **Rate-limit / outage noise.** |
| `lookup_failed` | Item or source not in DB. Should be 0 in steady state — non-zero means the seed didn't run or the watchlist YAML drifted. |

The unavailable-vs-declined split is ADR 013 §3. If you see `declined > 0` in steady-state, something's wrong upstream — see "Common failure modes" below.

## Source health SQL

When the logs look fine but you want to *verify* each source is actually writing prices, query directly:

```bash
docker compose exec -T postgres psql -U skinmarket -d skinmarket <<'SQL'
SELECT
    s.name AS source,
    count(*) AS rows_last_2h,
    max(p.timestamp)::timestamp(0) AS most_recent
FROM prices p
JOIN sources s ON s.id = p.source_id
WHERE p.timestamp > NOW() - INTERVAL '2 hours'
GROUP BY s.name
ORDER BY s.name;
SQL
```

What healthy looks like (assuming all sources enabled and at default intervals):

| Source | Interval | Expected rows/2h | Notes |
|---|---|---|---|
| `skinport` | 15min | ~30–40 | 8 cycles × ~5 written per cycle (after dedup) |
| `dmarket` | 15min | ~30–60 | Similar; volume varies with how many items move |
| `steam_market` | 60min | ~30–45 | 2 cycles × ~15–25 written (the ~10–14 ultra-rare-tail won't write) |

Numbers well below these mean either dedup is firing very high (quiet market — fine), or the source is rate-limited (see below), or the source is disabled in `sources` (`SELECT name, enabled FROM sources` to confirm).

**Currently-firing unavailability streaks** (per-item insight, ADR 013 + ADR 015):

```sql
SELECT
    i.market_hash_name,
    ins.meta_info->>'source_name' AS source,
    ins.value::int AS streak_cycles,
    ins.meta_info->>'last_seen_observed' AS last_seen
FROM insights ins
JOIN items i ON i.id = ins.item_id
WHERE ins.insight_type = 'item_unavailability_streak'
  AND ins.computed_at > NOW() - INTERVAL '90 minutes'
ORDER BY ins.value::int DESC, i.market_hash_name;
```

Expected (at v1 close): ~17 Steam streaks (the rare-tail + DMarket's 8 title-drop items + 1 Skinport item). Numbers significantly higher indicate a source is having broader issues.

## Common failure modes

### Steam rate-limit pause (normal — ADR 013 §2)

Symptom: bot log shows `Steam rate-limited (Retry-After=absent) — pausing job for 300s` followed by `steam_market job paused until …`. Steam cycles stop firing for the pause duration. After pause expires, one item is retried; if that also 429s, the doubling ladder kicks in (300s → 600s → 1200s → …, cap 1h).

This is correct behavior. **Don't intervene.** The collector self-recovers; the analytics layer surfaces the gap via `item_unavailability_streak`. Verify the ladder is working:

```bash
docker compose logs --since 2h collector | grep -E "rate-limited|paused"
```

If Steam stays paused for >2 hours and you need to force a retry: `docker compose restart collector` resets the in-memory ladder state.

### Skinport IP ban (manual recovery)

Symptom: Skinport cycles complete with `48 attempted, 0 written, 0 unchanged, 0 unavailable, 48 declined`. The bulk fetch is getting 429-banned at the IP level. Distinct from per-source rate-limit because Skinport's API uses a single bulk endpoint, so a 429 affects every item.

Recovery:

```bash
# 1. Disable Skinport to stop the retries (the scheduler honors sources.enabled).
docker compose exec -T postgres psql -U skinmarket -d skinmarket \
  -c "UPDATE sources SET enabled = false WHERE name = 'skinport';"

# 2. Wait. Skinport's IP-ban window has historically been hours, not days.
#    Test manually with curl from the host:
curl -sS -o /dev/null -w "%{http_code}\n" \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -H "Accept-Encoding: br, gzip" \
  "https://api.skinport.com/v1/items?app_id=730&currency=USD"
#    200 = back online. 429 = still banned, keep waiting.

# 3. Re-enable when curl returns 200:
docker compose exec -T postgres psql -U skinmarket -d skinmarket \
  -c "UPDATE sources SET enabled = true WHERE name = 'skinport';"
docker compose restart collector   # picks up the flag at startup
```

### DMarket title-match drops (expected — not a bug)

DMarket's `title=` query parameter does loose substring matching. For 8 watchlist items, it returns wrong-variant listings (e.g. `Desert Eagle | Oxide Blaze` for `Desert Eagle | Blaze`). The Phase 6.5 fix in `collectors/dmarket.py` enforces exact NFC-normalized title match and skips persistence on mismatch.

**Expected steady-state:** those 8 items show as `dmarket: never_observed` in the bot's three-state availability render and as item_unavailability_streak insights with `last_seen_observed: null`. This is correct behavior; DMarket can't observe them for us until DMarket fixes the substring-match.

The 8 items (commit `ec36cd9` for the full list): Desert Eagle | Blaze (FN), M4A1-S | Cyrex (FT), MP9 | Hot Rod (FN), SSG 08 | Death Strike (FN), and assorted Marble Fade and Doppler knife variants.

### Bot reply timeout / DeepSeek failure

Symptom: bot replies with `"I couldn't reach my DeepSeek LLM router right now"` or `"I had trouble answering that — could you try rephrasing?"` for queries the user knows should be answerable.

Diagnostics:

```bash
docker compose logs --since 5m bot | grep -E "ERROR|chat call failed|tool-call cap"
```

Three paths:

1. **DeepSeek unreachable or key missing** — `bot/deepseek_client.py` raised before a usable response. Check config without printing the key:
   ```bash
   docker compose run --rm bot python -c \
     "from bot.deepseek_client import validate_config; validate_config(); print('ok')"
   ```

2. **Usage logging failed** — DeepSeek returned, but the bot could not insert into `llm_usage_log`. Confirm Alembic is at `0011` and the table exists:
   ```bash
   uv run alembic current
   uv run python - <<'PY'
   from sqlalchemy import text
   from db.connection import get_engine
   with get_engine().connect() as c:
       print(c.execute(text("select count(*) from llm_usage_log")).scalar_one())
   PY
   ```

3. **Tool-call cap hit** — the LLM is looping. Tell the user to rephrase more concretely (e.g. include the exact item name). If it persists across queries, inspect the recent `tool_calls` path in the bot logs.

Size discipline (ADR 016 §11, ADR 026) prevents the original Phase 7c failure mode where unbounded tool results made the LLM spend too much time and too many tokens rendering bulk JSON. If you suspect a NEW tool is shipping unbounded data:

```bash
# Find recent ReadTimeouts in bot logs:
docker compose logs --since 1h bot | grep -i timeout
```

The tool functions in `bot/tools.py` carry the size-discipline contract; reaffirm it when adding a new tool.

## Token rotation

### API bearer token

The api accepts a set of tokens (Phase 7b multi-token design, ADR 014 §10). To rotate without breaking the bot:

```bash
NEW_TOKEN=$(openssl rand -hex 32)

# Add the new token alongside the old one in .env. The plural var
# accepts comma-separated values; both old and new authenticate
# during the cutover window.
# In .env:
#   SKIN_MARKET_API_TOKEN=<old token>           ← keep for now
#   SKIN_MARKET_API_TOKENS=<old>,<new>          ← add the plural form

docker compose up -d api    # api accepts both
# Test:
curl -sS -H "Authorization: Bearer $NEW_TOKEN" http://localhost:8001/items | head

# Update the bot's env var to the new token:
# .env: SKIN_MARKET_API_TOKEN=<new>
docker compose up -d bot

# Drop the old token from SKIN_MARKET_API_TOKENS:
# .env: SKIN_MARKET_API_TOKENS=<new>
docker compose up -d api
```

### Discord bot token

If the Discord token leaks (or you just want to rotate):

1. Discord developer portal → your application → Bot → **Reset Token**. Copy the new token.
2. Update `DISCORD_BOT_TOKEN` in `.env`.
3. `docker compose up -d bot`.

Discord invalidates the old token immediately on reset. There's no graceful cutover window.

## Test workflow

```bash
uv run pytest          # default; skips destructive tests
uv run pytest -m destructive   # WIPES collected price + insight data
uv run ruff check .    # linter
```

Default run is safe to execute against any DB state. Destructive opt-in exists for the migration-roundtrip test only (it drops + recreates all domain tables, then re-seeds items/sources). Don't run `-m destructive` against a DB you care about the data in.

## Postgres password rotation footgun

`POSTGRES_PASSWORD` in `.env` is only consumed by the postgres container on the **first init of the data volume**. After that:

- Changing `.env`'s password does NOT update the running postgres.
- The container keeps accepting the OLD password.
- Other services read the NEW password from compose at startup and fail to authenticate.

Symptom: collector/api/analytics logs show `psycopg.OperationalError: password authentication failed for user "skinmarket"`. Postgres logs show `FATAL: password authentication failed for user`.

Recovery — pick one:

**Option 1 (recommended): ALTER USER in the running container.**

```bash
docker exec -it skinmarket-postgres psql -U skinmarket -d skinmarket \
  -c "ALTER USER skinmarket WITH PASSWORD 'new_password_from_env'"
docker compose restart collector api analytics bot
```

**Option 2 (destructive): drop the data volume.**

```bash
docker compose down -v   # WIPES all collected price + insight data
docker compose up -d     # re-inits postgres from .env's new password
```

Option 1 unless you actually wanted to start fresh.

## Related documentation

- `README.md` — what this project is + 15-minute onboarding
- `PROJECT_SPEC.md` — what each phase builds + acceptance criteria
- `ARCHITECTURE.md` — workflow rules, libraries-of-choice, anti-patterns
- `docs/sources-and-semantics.md` — the denomination invariant (USD vs Steam wallet credit; why we never average)
- `docs/adr/*.md` — Architecture Decision Records, chronological. The ones most relevant to operations:
  - 002 — TimescaleDB compression policy (30-day threshold)
  - 006 — Collector resilience (per-HTTP-outcome behavior)
  - 009 — Scheduler design (overlap policy, dedup, SIGTERM)
  - 013 — Rate-limit policy (DB-driven, Retry-After, declined/unavailable split, observation_log)
  - 014 — Read API design (money-as-string, denomination tagging, multi-token auth, `/health` bypass)
  - 016 — Bot runtime (tool-use loop, size discipline)
  - 026 — DeepSeek inference hard cutover and usage accounting
