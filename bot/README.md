# Discord bot — operator install + workflow

The Phase 7c bot. Runs as a docker-compose service (`bot`) using the same image as the rest of the stack, makes outbound calls to (a) Discord, (b) the local `api` container, (c) Ollama on the host. No inbound network exposure.

The bot's design and the rationale for every choice are in `docs/adr/016-discord-bot-runtime.md`. This file is the operator-facing walkthrough.

## Prerequisites

- Phase 6+ stack already running (`docker compose ps` should show `postgres`, `collector`, `analytics`, `api` all `Up (healthy)` or running).
- The api container's `SKIN_MARKET_API_TOKEN` set and known (Phase 6.6 — see `.env`).
- Ollama running on the host with `huihui_ai/Qwen3.6-abliterated:27b` pulled. Verify:
  ```bash
  curl http://localhost:11434/api/tags | jq '.models[].name' | grep Qwen3.6
  ```

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

# Already set from Phase 6.6 — leave as is:
SKIN_MARKET_API_TOKEN=<unchanged>

# Already set from Phase 5 — leave as is:
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=huihui_ai/Qwen3.6-abliterated:27b
```

Empty `DISCORD_ALLOWED_USER_IDS` is a valid config but the bot will refuse every message with "I'm not configured for any users yet" — that's the fail-closed default.

## Step 5 — Verify reachability before launching

```bash
# Verify Ollama is reachable from inside the bot container's network:
docker compose run --rm bot python -c \
  "import httpx; print(httpx.get('http://host.docker.internal:11434/api/tags', timeout=3).status_code)"
# Expect: 200

# Verify the api is reachable (it should already be running):
docker compose ps api
# Expect: Up (healthy)
```

If Ollama fails, check that the daemon is running on the host (`ollama serve` or systemd unit) and that `huihui_ai/Qwen3.6-abliterated:27b` is pulled (`ollama list`).

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
2. After 2–30 seconds (longer on the first call after Ollama starts, then ~1–3s under KEEP_ALIVE), the bot replies with the per-source price snapshot.
3. The reply renders all three sources with denomination tags, the SC-credit footnote on first occurrence, and any anomaly flag if a divergence is active.

If the reply is wrong or weird, check `docker compose logs -f bot` — the bot logs every Ollama interaction at INFO level.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bot doesn't reply at all in a channel | MESSAGE_CONTENT intent not enabled in developer portal | Enable it (step 1.5) and restart bot. |
| Bot replies "I'm not authorized to chat with you" | Your user ID isn't in `DISCORD_ALLOWED_USER_IDS` | Add it, `docker compose up -d bot`. |
| Bot replies "I'm not configured for any users yet" | Empty `DISCORD_ALLOWED_USER_IDS` | Fill it in. |
| Bot replies "I couldn't reach my local LLM router" | Ollama down on the host, or `host.docker.internal` not resolving | Check `ollama serve`; on Linux Docker, verify the compose service has `extra_hosts: ["host.docker.internal:host-gateway"]` (it should — already in the bot service definition). |
| Bot replies "Auth … API rejected the bearer token (401)" | `SKIN_MARKET_API_TOKEN` in the bot's env doesn't match the api's accepted set | Sync them; `docker compose up -d bot api`. |
| Bot replies "I had trouble answering that — try rephrasing?" | The LLM hit the tool-call cap (5 rounds) without producing text | Open-source model misroute; ask a more specific question or restart Ollama. |
| First reply takes 30+ seconds | Cold model load (Qwen 27b) | Normal. Subsequent calls under KEEP_ALIVE are fast. The Ollama timeout is 120s. |

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
# Edit OLLAMA_MODEL in .env.
# 1. Pull the new model: ollama pull <name>
# 2. docker compose up -d bot
# (No rebuild needed — model name is just an env var.)
```

## Out of scope at v1

- **Slash commands** (`/price ak-redline-ft`) — Phase 8+.
- **Conversation memory** — each Discord message is independent context.
- **Per-server config** — single-tenant; the bot serves any server it's been invited to with the same backend + allowlist.
- **Adding items to the watchlist via the bot** — operator CLI only (`scripts/watchlist_edit.py`).
- **Cloud LLM fallback** — if Ollama is down, the bot says so; no automatic failover.

## See also

- `docs/adr/016-discord-bot-runtime.md` — full design rationale.
- `docs/adr/015-bot-skill-design.md` — the Phase 7b Hermes design, which still governs the bot's rendering rules (denomination, three-state availability, error matrix).
- `docs/archive/bot_skill_hermes_attempt/` — the original Hermes scaffolding, preserved for reference.
