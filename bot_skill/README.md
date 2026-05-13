# skin-market — Hermes skill (operator README)

This directory is a Hermes skill bundle that gives a Discord bot the ability to answer CS2 skin-market questions backed by the local `skin-market` data pipeline.

- `SKILL.md` — what Hermes' LLM reads at session start (tool descriptions, rendering rules, error matrix).
- `tools.py` — the seven Python tool functions the LLM can call.
- `README.md` — this file (operator instructions).

## What the bot can do

Seven tools:

1. **`list_watchlist`** — what items are tracked
2. **`query_current_price`** — three-state per-source snapshot for one item (the workhorse)
3. **`query_price_history`** — time-series for one item, optional source filter
4. **`render_chart`** — PNG chart, single source, N days
5. **`evaluate_deal`** — opinionated verdict on a price offer
6. **`narrative_today`** — the latest daily English-prose recap
7. **`whats_interesting`** — currently-firing anomalies (divergence + volume)

See `SKILL.md` for details, rendering rules, and the architectural invariants (especially denomination tagging — Steam Wallet credit ≠ USD).

## Install

The skill expects to live at `~/.hermes-discord/skills/skin-market/`. Symlink (so edits in the repo propagate immediately) or copy:

```bash
# Option A — symlink (recommended for development):
ln -s /home/vasilis/skin-market/bot_skill ~/.hermes-discord/skills/skin-market

# Option B — copy (if your Hermes loader doesn't follow symlinks):
mkdir -p ~/.hermes-discord/skills/skin-market
cp -r /home/vasilis/skin-market/bot_skill/* ~/.hermes-discord/skills/skin-market/
```

The repo's path may differ on your host; replace `/home/vasilis/skin-market` accordingly.

## Token sharing

The tools call `http://localhost:8001` (the api container's host-port mapping from Phase 6.6) with a static bearer token. The token must match one of the api container's accepted tokens.

The simplest recipe:

```bash
# In the api repo:
grep SKIN_MARKET_API_TOKEN /home/vasilis/skin-market/.env
# Copy the value.

# In Hermes' env file (location depends on your Hermes setup; common path is ~/.hermes-discord/.env):
echo "SKIN_MARKET_API_TOKEN=<the value from above>" >> ~/.hermes-discord/.env
```

Then restart Hermes (its skill loader picks up the new env variable on next startup).

### Multi-token deployment

The api accepts multiple tokens — useful when adding a second consumer (e.g. an operator CLI alongside the bot) without disrupting the bot's existing token. ADR 014 §10 has the full design; the short version:

```bash
# In the api repo's .env, replace the single-token line with the plural form:
SKIN_MARKET_API_TOKENS=<bot-token>,<operator-cli-token>

# Or keep both forms; the api takes the union of both vars.
SKIN_MARKET_API_TOKEN=<bot-token>
SKIN_MARKET_API_TOKENS=<operator-cli-token>
```

Then `docker compose up -d api` (re-reads `.env`).

The bot only needs to know its own `SKIN_MARKET_API_TOKEN`; the api decides whether to accept it.

### Token rotation

1. Generate a new token: `openssl rand -hex 32`
2. Add it to the api's `SKIN_MARKET_API_TOKENS` (alongside the old one, so the bot keeps working during the cutover): `SKIN_MARKET_API_TOKENS=<old>,<new>`
3. `docker compose up -d api` — api now accepts both old and new
4. Update the bot's `SKIN_MARKET_API_TOKEN=<new>` in Hermes' env file, restart Hermes
5. Remove the old token from `SKIN_MARKET_API_TOKENS`, `docker compose up -d api`

No tooling for this in v1 — manual is fine while there's one bot.

## Custom API base URL

If you run the api on a non-default host or port (e.g. behind a reverse proxy, or with a different `ports:` mapping), set:

```bash
SKIN_MARKET_API_BASE_URL=http://localhost:8001
```

Default is `http://localhost:8001`. The bot only needs a URL it can reach.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot reply: *"Auth between me and the market service is misconfigured."* | `SKIN_MARKET_API_TOKEN` not set in Hermes' env, or doesn't match the api's set | Verify the env var is set in Hermes; copy the value from the api's `.env`; restart Hermes. |
| Bot reply: *"Market data service is unreachable."* | `docker compose ps api` shows the container down, or you're hitting the wrong port | Check `docker compose ps api`; check `SKIN_MARKET_API_BASE_URL` matches the api's host-port mapping (Phase 6.6 uses `127.0.0.1:8001:8000`). |
| Bot says items are unavailable that you know exist | System warmed up recently (Phase 7a's `observation_log` is being populated by collector cycles) | Wait 2–3 collector cycles (~30–45 min); the streak counter resolves naturally. |
| New tool added to `tools.py` doesn't show up in Hermes | Hermes' loader cached the previous registry | Restart Hermes; if that doesn't work, check that the new tool has the `@tool` decorator and a non-empty docstring (loader heuristic per `test_every_tool_has_docstring`). |
| 401 on `/health` checks from Discord | `/health` is supposed to be unauthenticated; the bot is misrouting health probes through the authenticated path | The bot should not call `/health` — that's for Docker. If you need a liveness check from the bot side, call `list_watchlist()`. |

## Reading the bot's behavior

The tools log to stderr at INFO level. To see what the bot is actually calling:

```bash
# Hermes log location varies; common path:
tail -f ~/.hermes-discord/logs/discord-bot.log | grep skin_market
```

For deeper debugging, raise the bot's log level to DEBUG; the tool wrappers don't add their own DEBUG output, but `httpx` will log every request and response.

## When to update this skill

- **New API endpoint exposed** — add a new tool in `tools.py` + describe it in `SKILL.md`'s table. Tests in `tests/test_bot_skill.py`.
- **Fourth source lands** (e.g. CSFloat) — update `EXPECTED_SOURCES` and `_DENOMINATION_BY_SOURCE` in `tools.py`. SKILL.md's three-state list grows by one row.
- **Rendering rules change** — edit SKILL.md only; tool code returns structured data and is rendering-agnostic.

## See also

- `docs/adr/015-bot-skill-design.md` — full design rationale: bot scope, denomination rules, three-state availability, Hermes integration shape, auth model.
- `docs/sources-and-semantics.md` — why per-source prices stay distinct (Steam wallet credit vs USD).
- `docs/adr/014-read-api-design.md` §10 — api auth model the bot is wired to.
