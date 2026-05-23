# Discord bot — operator install + workflow

The Phase 7c bot. Runs as a docker-compose service (`bot`) using the same image as the rest of the stack, makes outbound calls to (a) Discord, (b) the local `api` container, and (c) DeepSeek. It writes one `llm_usage_log` row per DeepSeek request. No inbound network exposure.

The bot's design is in `docs/adr/016-discord-bot-runtime.md`; the DeepSeek cutover and pricing are in `docs/adr/026-deepseek-inference-cutover.md`. This file is the operator-facing walkthrough.

## Prerequisites

- Phase 6+ stack already running (`docker compose ps` should show `postgres`, `collector`, `analytics`, `api` all `Up (healthy)` or running).
- The api container's `SKIN_MARKET_API_TOKEN` set and known (Phase 6.6 — see `.env`).
- `DEEPSEEK_API_KEY` set in `.env`.

## Step 1 — Create the Discord application + bot

1. Go to https://discord.com/developers/applications.
2. **New Application** → give it a name (e.g. `skin-market`).
3. In the left sidebar, click **Bot**.
4. **Reset Token** → copy the new token. Treat this as a secret on par with a password — a leaked token lets anyone impersonate the bot.
5. Scroll down to **Privileged Gateway Intents** and toggle **MESSAGE CONTENT INTENT** on. This is required since 2022 to read message bodies; the bot will see empty `message.content` if you skip this.
6. Save changes.

## Step 2 — Invite the bot to a Discord server

1. In the application's left sidebar, click **OAuth2** → **URL Generator**.
2. Under **Scopes**, check `bot`.
3. Under **Bot Permissions**, check:
   - Read Messages/View Channels
   - Send Messages
   - Send Messages in Threads (optional — for thread replies)
   - Attach Files (required for `render_chart`)
   - Read Message History
4. Copy the generated URL at the bottom, open it in a browser, and authorize the bot for a server you administer.

## Step 3 — Find your Discord user ID

1. In Discord, open **Settings** → **Advanced** → toggle **Developer Mode** on.
2. Right-click your username anywhere → **Copy User ID**. This is a long decimal string (e.g. `123456789012345678`).

If multiple people will use the bot, collect each person's ID.

## Step 4 — Edit `.env`

```bash
# In the repo root .env file:
DISCORD_BOT_TOKEN=<the token from step 1>
DISCORD_ALLOWED_USER_IDS=<your user ID>          # single user
# or
DISCORD_ALLOWED_USER_IDS=<id1>,<id2>,<id3>       # multiple users
PRICE_ALERT_POLL_SECONDS=60
PRICE_ALERT_BATCH_LIMIT=100
PRICE_ALERT_MAX_ACTIVE_PER_USER=25
PRICE_ALERT_MAX_DELIVERY_ATTEMPTS=5

# Already set from Phase 6.6 — leave as is:
SKIN_MARKET_API_TOKEN=<unchanged>

# LLM backend:
DEEPSEEK_API_KEY=<DeepSeek API key>
DEEPSEEK_MODEL=deepseek-v4-flash
LLM_USAGE_LOG_FULL_PROMPT=false
DEEPSEEK_DAILY_COST_LIMIT_USD=0       # optional global 24h cap; 0 disables
DEEPSEEK_DAILY_USER_COST_LIMIT_USD=0  # optional per-user 24h cap; 0 disables
```

Empty `DISCORD_ALLOWED_USER_IDS` is a valid config but the bot will refuse every message with "I'm not configured for any users yet" — that's the fail-closed default.

## Step 5 — Verify reachability before launching

```bash
# Verify DeepSeek config is present without printing the key:
docker compose run --rm bot python -c \
  "from bot.deepseek_client import validate_config; validate_config(); print('ok')"
# Expect: ok

# Verify the api is reachable (it should already be running):
docker compose ps api
# Expect: Up (healthy)
```

If this fails, set `DEEPSEEK_API_KEY` in `.env` and recreate the bot container.

## Step 6 — Launch the bot

```bash
docker compose build bot
docker compose up -d bot
docker compose logs --tail 50 -f bot
```

You should see lines like:
```
Bot connected as <bot-name> (id=…); allowlist size=1
```

If `allowlist size=0`, you forgot to set `DISCORD_ALLOWED_USER_IDS`.

## Step 7 — Test in Discord

In any channel the bot has access to (or in a DM to the bot):

```
@skin-market what's the AK Redline FT price?
```

Expected behavior:

1. The bot shows a "typing…" indicator.
2. The bot replies with the per-source price snapshot.
3. The reply renders all three sources with denomination tags, the SC-credit footnote on first occurrence, and any anomaly flag if a divergence is active.

If the reply is wrong or weird, check `docker compose logs -f bot`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bot doesn't reply at all in a channel | MESSAGE_CONTENT intent not enabled in developer portal | Enable it (step 1.5) and restart bot. |
| Bot replies "I'm not authorized to chat with you" | Your user ID isn't in `DISCORD_ALLOWED_USER_IDS` | Add it, `docker compose up -d bot`. |
| Bot replies "I'm not configured for any users yet" | Empty `DISCORD_ALLOWED_USER_IDS` | Fill it in. |
| Bot replies "I couldn't reach my DeepSeek LLM router" | Missing key, network/API failure, or usage logging failure | Check `DEEPSEEK_API_KEY`, outbound network, and `llm_usage_log` migration state. |
| Bot replies "Auth … API rejected the bearer token (401)" | `SKIN_MARKET_API_TOKEN` in the bot's env doesn't match the api's accepted set | Sync them; `docker compose up -d bot api`. |
| Bot replies "I had trouble answering that — try rephrasing?" | The LLM hit the tool-call cap (5 rounds) without producing text | Ask a more specific question and inspect bot logs. |

## Operator commands (not bot commands)

Adding a user:
```bash
# Edit .env to extend DISCORD_ALLOWED_USER_IDS
docker compose up -d bot
```

Removing a user:
```bash
# Edit .env to drop the ID
docker compose up -d bot
```

Rotating the Discord bot token (e.g. after a leak):
```bash
# 1. Reset the token in the developer portal (step 1.4).
# 2. Update DISCORD_BOT_TOKEN in .env.
# 3. docker compose up -d bot
```

Changing the LLM model:
```bash
# Edit DEEPSEEK_MODEL in .env.
# docker compose up -d bot analytics
# (No rebuild needed — model name is just an env var.)
```

## Out of scope at v1

- **Slash commands** (`/price ak-redline-ft`) — Phase 8+.
- **Conversation memory** — each Discord message is independent context.
- **Per-server config** — single-tenant; the bot serves any server it's been invited to with the same backend + allowlist.
- **Adding items to the watchlist via the bot** — operator CLI only (`scripts/watchlist_edit.py`).
- **LLM fallback** — if DeepSeek is unavailable, the bot says so; no automatic failover.

## See also

- `docs/adr/016-discord-bot-runtime.md` — full design rationale.
- `docs/adr/026-deepseek-inference-cutover.md` — DeepSeek model, pricing, and usage accounting.
- `docs/adr/015-bot-skill-design.md` — the Phase 7b Hermes design, which still governs the bot's rendering rules (denomination, three-state availability, error matrix).
- `docs/archive/bot_skill_hermes_attempt/` — the original Hermes scaffolding, preserved for reference.
