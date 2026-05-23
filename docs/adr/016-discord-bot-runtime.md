# ADR 016 — Discord bot runtime (Phase 7c)

**Status:** Superseded for inference backend by ADR 026; accepted for
tool-loop, rendering, allowlist, and size-discipline design.
**Date:** 2026-05-13
**Related:** ADR 011 (narrative LLM job, local Ollama), ADR 014 §10 (api auth), ADR 015 (Phase 7b Hermes attempt; superseded by this ADR for the runtime layer; the design rules in ADR 015 — denomination, three-state, rendering matrix — are still authoritative for the bot's behavior)

## 2026-05-23 Amendment — DeepSeek hard cutover

ADR 026 replaces the local Ollama inference backend with DeepSeek's
OpenAI-format chat API. The retained parts of this ADR are the Discord
bot shape, the bounded tool-use loop, tool-result size discipline,
defensive malformed-tool handling, allowlist, trigger policy, and chart
attachment flow. The load-bearing Ollama-specific choice ("Default" vs
"Native", Qwen3.6-abliterated, `OLLAMA_*` env vars, and local timeout
tuning) is historical only.

The current backend file is `bot/deepseek_client.py`; it preserves the
same tool loop and returns one `BotReply`, but sends requests to
DeepSeek, disables thinking mode for token efficiency, and logs every
DeepSeek request to `llm_usage_log`.

## Context

Phase 7b shipped a Hermes-shaped skill bundle (`bot_skill/` — now archived at `docs/archive/bot_skill_hermes_attempt/`) on the assumption that an external Hermes Discord skill loader would consume it. Phase 7c contact: there is no Hermes runtime on the Spark, and we don't actually need one — what we need is a Discord bot that uses the seven skin-market tools and posts answers in a Discord server. Phase 7c replaces the Hermes scaffolding with a focused custom bot, retains every tool's function body, and routes LLM tool-calling through the local Ollama daemon already running on the Spark for the analytics narrative job (ADR 011).

The bot is the last user-facing piece of the v1 stack; everything from Phase 6 onward existed in order to feed it.

## Decisions

### 1. Local Ollama over a cloud LLM

The bot's LLM router is the same Ollama daemon (`http://host.docker.internal:11434`) that the analytics narrative job uses. Default model: `huihui_ai/Qwen3.6-abliterated:27b`.

**Why local:**

- **Project posture.** ADR 011 already commits to local Ollama for the narrative job for the same reasons that apply here: privacy (no message content leaves the box), no per-message cost, no external dependency that breaks if a vendor changes pricing or API shape.
- **Already loaded on the Spark.** Qwen3.6 abliterated 27b is the model we know works for tool calling on this hardware. Adding a second model class for the bot would mean another GB of VRAM in use.
- **Consistent with the rest of the project's local-first stance.** Postgres on the Spark. Collectors on the Spark. API on the Spark. The bot's LLM on the Spark.

**The asymmetry we accept:** open-source tool calling is less reliable than Anthropic's or OpenAI's tool-use APIs. Models occasionally produce malformed argument JSON, call nonexistent tools, or get stuck looping. §6 below documents how the bot defends against this.

**Where this could change:** if open-source tool-calling reliability becomes a sustained problem (e.g. the model misroutes one query in five), `bot/ollama_client.py` is the swappable seam. The rest of `bot/` (tools, discord_render, main) is router-agnostic — a future cloud-LLM router would replace one file. Not on the v1 roadmap.

### 2. Custom Discord bot over an existing framework

Hermes-on-Spark wasn't an option. The alternatives were either (a) install some other Discord-bot framework's plugin system and adapt our tool functions to its conventions, or (b) write a focused bot that does only what we need. We picked (b).

**Why custom:**

- The bot's surface is small: one event handler (`on_message`), one router call (`handle_user_message`), one rendering helper. ~250 lines total.
- A framework's plugin conventions would have been a learning curve and an unknown for future maintenance, with no offsetting feature gain.
- discord.py is well-documented, well-supported, and the de-facto Python Discord client. Using it directly is less foreign than learning a wrapper-on-top.

### 3. Qwen3.6-abliterated:27b + the Default chat endpoint (load-bearing)

The model is `huihui_ai/Qwen3.6-abliterated:27b`. Tool calling goes through Ollama's **standard chat-completion endpoint** with `tools=[…]` in the request payload (the "Default" path in Open WebUI terms), via `ollama.AsyncClient.chat(model=…, messages=…, tools=…)`. This is the path Phase 7c uses.

**The path NOT to use** is any "Native tool calling" variant — Ollama exposes a separate flow for that, and for the Qwen3-abliterated family it does not work reliably in our testing. This is documented in project memory and called out here because it's the kind of choice that breaks silently if a future maintainer "cleans up" by switching to what looks like the more modern path.

**Why abliterated:** Steam-market analysis includes named CS2 items the base model often refuses or hedges on. The abliterated variant aligns with project preference; aligned variants over-refuse for a chat use case where the user is asking about market prices.

**Not chosen:** DeepSeek R1 70B abliterated. It uses native `<think>` tags inline in its output. Those would be in-band with the bot's reply and would either leak to Discord users or require tag-stripping logic that's easy to get wrong. Qwen3.6 stays clean.

### 4. Tool-use loop shape

`handle_user_message` is an async function that runs a bounded multi-turn conversation:

```
1. messages = [system_prompt, user_message]
2. response = await ollama.chat(messages, tools=TOOL_DEFINITIONS)
3. if response has no tool_calls → return text reply
4. for each tool_call:
     - execute the tool via asyncio.to_thread (tool bodies are sync I/O)
     - append the tool_result to messages
5. goto 2, capped at MAX_TOOL_CALLS rounds
```

**Why `asyncio.to_thread`:** the tool bodies use `httpx.Client` (sync) against the API. Calling them inside the discord.py event loop would block other Discord events. `to_thread` runs each tool on a worker thread; the event loop stays responsive.

**MAX_TOOL_CALLS = 5.** Open-source models occasionally loop calling the same tool with slightly different arguments. The cap prevents runaway. When hit, the bot replies "I had trouble answering that — could you try rephrasing?" and logs the conversation.

**Sequential, not parallel.** The model can request multiple tool calls in one response; we execute them sequentially. Parallelism would complicate the tool_result ordering and isn't a meaningful latency improvement at v1 scale.

### 5. Tool registration shape (changed from Phase 7b)

Phase 7b registered tools with a `@tool` decorator and a `TOOLS` list. Phase 7c registers them as **JSON-schema dicts in `TOOL_DEFINITIONS`** (Ollama's request format) and a **parallel `TOOL_FUNCTIONS` dict** mapping `name → callable` for the executor to dispatch by name.

The function bodies are unchanged from Phase 7b — same HTTP wrappers, same typed exceptions, same three-state composer in `query_current_price`. Only the registration glue changed.

JSON-schema descriptions lean heavily on **concrete trigger phrases** ("Call this when the user asks 'X'"). Open-source models need explicit routing guidance more than cloud APIs do (§6). A test (`test_every_definition_has_concrete_trigger_examples`) guards against descriptions drifting away from this shape.

### 6. Defensive handling for malformed tool calls

The bot survives every failure mode we've seen:

| Failure mode | Detection | Recovery |
|---|---|---|
| Arguments returned as a JSON string instead of dict | `_normalize_arguments` parses; on `JSONDecodeError`, returns `{}`. | Tool may raise `TypeError` for missing args; that's caught and surfaced as a tool_result. |
| Arguments as a string that isn't even JSON | `_normalize_arguments` returns `{}`. | Same. |
| Unknown tool name | `TOOL_FUNCTIONS.get(name) is None` check before execution. | tool_result says "no tool called X exists; pick one of …"; model can recover. |
| Tool raises a typed `SkinMarketBotError` | Caught around the tool execution. | `str(exc)` becomes the tool_result; model phrases a user-facing reply. |
| Tool raises an unexpected exception | Caught broadly; logged with `logger.exception`. | tool_result says "internal error; check bot logs". |
| Model loops calling the same tool indefinitely | `MAX_TOOL_CALLS` cap. | Bot returns the canned "try rephrasing?" reply. |
| Ollama itself unreachable / errors | Caught around the `client.chat` call. | Bot returns a user-presentable "I couldn't reach my local LLM router". |

**Architectural rule:** NO tool failure should propagate up to discord.py and crash the message handler. Every error path produces a graceful Discord reply. Tests in `tests/test_bot.py::TestOllamaClientDefensive` exercise each row of this table.

### 7. User allowlist as defense-in-depth

`DISCORD_ALLOWED_USER_IDS` in `.env` — comma-separated decimal Discord user IDs. Only listed users get replies. Empty allowlist rejects everyone with a config-error message ("not configured for any users yet").

**Why an allowlist:**

- Discord bot tokens leak. If an invite URL gets shared (or the token is exfiltrated), an unwanted server could add the bot — without an allowlist, anyone could query it.
- The bot has no user-tier auth (ARCHITECTURE.md defers that to v5+); allowlist is the cheap version that blocks accidental abuse.
- Costs: a leaked token still lets the attacker do "everything the bot can do to a Discord server the bot was invited to" (read messages, send messages). The allowlist limits who can use the *bot's* tools; it doesn't limit Discord-side actions the token could enable.

**Failure semantics:**

- Disallowed user: gets exactly one "not authorized" reply per process lifetime. Further messages from the same user are silently ignored (an in-memory `suppressed_users` set, scoped to the process). Restart of the bot resets the suppression.
- Empty allowlist: every message returns the config-error reply (fail closed; default-deny).

**Not auth-grade:** Discord user IDs are public; an attacker who knows an allowed ID could try to spoof… except they can't, because Discord's gateway authenticates the message author. So the allowlist is effectively as secure as Discord's gateway authentication, which is fine for v1.

### 8. Triggering: @-mention and DMs only

The bot responds to two message kinds:

- **DM** to the bot (no `guild`).
- **@-mention** in a guild channel (`client.user in message.mentions`).

It does NOT respond to non-addressed messages. No passive listening. No automatic posting. This is reinforced by ARCHITECTURE.md §"out of scope" — no passive logging of non-addressed messages.

The discord.py `MESSAGE_CONTENT` intent is required to read message bodies. It must be enabled BOTH in code (`intents.message_content = True`) AND in the Discord developer portal under Privileged Gateway Intents. `bot/README.md` documents both.

### 9. Chart attachment flow

When the model calls `render_chart`:

1. The tool returns an `Attachment` dataclass (PNG bytes + filename + media_type).
2. `ollama_client._execute_tool` notices `isinstance(result, Attachment)` and stashes it on the response object alongside a synthetic tool_result text ("Chart generated successfully and attached…").
3. The model sees the tool_result and writes a short text comment ("Here's the 7-day chart for X").
4. `bot/main.py`'s message handler converts the attachment to `discord.File` via `bot.discord_render.attachment_to_file` and passes it to `channel.send(file=...)`.

Multiple chart calls in one conversation are rare; we keep only the most recent attachment. The model is told (in `system_prompt.py`) that the chart is attached automatically and to add a one-line text comment.

### 10. What's NOT in scope at v1

- **Slash commands.** Phase 8+. v1 is DM and @-mention only.
- **Persistent conversation memory.** Each Discord message is fresh context. The bot doesn't remember past conversations across messages. If you ask "and what about M4A4 Howl?", the bot has no idea you were just discussing AK Redline.
- **Per-server data isolation.** Single-tenant — the bot serves whatever Discord servers it's invited to, all reading the same backend.
- **Cloud LLM fallback.** If Ollama is down, the bot replies "I couldn't reach my local LLM router". No automatic failover.
- **Threading.** A long reply doesn't open a thread; it's just one (truncated at 2000 chars) message.
- **User watchlist editing.** Same scope exclusion as ADR 015 §1. Operator-only via `scripts/watchlist_edit.py`.

## Operator workflow summary

See `bot/README.md` for the full walkthrough. The short version:

1. Create a Discord application + bot at https://discord.com/developers/applications, copy the bot token.
2. In the bot's Privileged Gateway Intents, enable MESSAGE CONTENT INTENT.
3. Generate an OAuth invite URL with scopes `bot` + permissions Read Messages / Send Messages / Attach Files / Read Message History.
4. Invite the bot to a Discord server.
5. With Developer Mode on (Discord Settings → Advanced), right-click your name → Copy User ID.
6. Edit `.env`: `DISCORD_BOT_TOKEN=…`, `DISCORD_ALLOWED_USER_IDS=<your id>`.
7. Verify Ollama is reachable from the bot service.
8. `docker compose up -d bot`.
9. @-mention the bot in Discord: "@bot what's the AK Redline FT price?"

### 11. Tool result size discipline (load-bearing)

**Phase 7c-fix amendment.** End-to-end testing surfaced a class of bug the original ADR didn't cover: tool results that return unbounded structured data blow past the Ollama timeout because Qwen3 27b spends real wall-clock time *rendering* the data into prose. The failing case: `"what items do you track?"` → `list_watchlist` → 48-item list returned to LLM → >120s rendering → `ReadTimeout`. Queries returning one-item shapes (single price, single verdict, chart PNG) completed in 46-95s; the failure mode is specifically size-driven.

**The wrong framing** was "bump the timeout". The right framing: **the LLM is not a generic renderer for structured data.** It's for routing and natural-language phrasing. Tool results going to the LLM must be bounded *by the bot*, not by the model's effort budget.

**Size-discipline rule, applied in `bot/tools.py`:**

| Tool | Size discipline |
|---|---|
| `list_watchlist` | Always summarized to `{count, by_category, sample}`. Categorization via display_name regex; first 5 items as sample. Never returns the raw 48-record list. |
| `query_price_history` | Pass-through when ≤30 rows. Above 30, return `{count, downsampled: true, per_source_stats: {source: {first_price, first_observed, last_price, last_observed, min_price, max_price, count, denomination}}}`. The LLM can answer "how has X moved?" from the aggregates; raw points aren't needed. |
| `whats_interesting` | Pass-through when ≤10 anomalies. Above 10, top-N by `|z-score|` + `total_count`. The LLM mentions "X anomalies total; here are the most severe". |
| `narrative_today` | `text` passes through (one paragraph, bounded by design). `meta` collapsed from citation lists to `{as_of, cited_count}` — the LLM doesn't need the citation rows to render the paragraph. |
| `query_current_price`, `render_chart`, `evaluate_deal` | Already bounded by their domain shape (3 sources, 1 chart, 1 verdict). Unchanged. |

The thresholds are tunable constants in `bot/tools.py`: `HISTORY_DOWNSAMPLE_THRESHOLD = 30`, `ANOMALIES_TOP_N_THRESHOLD = 10`, `WATCHLIST_SAMPLE_SIZE = 5`. The size-discipline tests in `tests/test_bot.py::TestListWatchlistSizeDiscipline` / `TestQueryPriceHistorySizeDiscipline` / `TestWhatsInterestingSizeDiscipline` / `TestNarrativeMetaTrimmed` regression-guard the shape contract.

**The end-to-end regression test** (`TestEndToEndWhatDoYouTrackPayloadBounded`) reconstructs the exact failure path: 48 items mocked → `list_watchlist` called → `tool_result` content asserted under 2KB. This is the kind of bug that's easy to re-introduce by tweaking a tool's return shape; the test catches it before it ships.

**Defensive band-aid:** `OLLAMA_TIMEOUT_SECONDS` bumped from 120 → 300 in `bot/ollama_client.py`. This is not the fix; it's the headroom for legitimate large-response cases the size discipline can't fully eliminate (e.g. a narrative with substantial prose, history queries on volatile items). The fix is size discipline.

**Future work (not v1):** bypass-LLM rendering for list-shaped responses. For tools where the natural output is a deterministic list (full watchlist enumeration, history table dumps), `bot/discord_render.py` would render directly to Discord markdown without round-tripping through the LLM for formatting. The LLM still chooses *which* tool to call; it just doesn't re-render output the bot can render faithfully on its own. This is more invasive than size-discipline (it changes the response surface between `ollama_client` and `main`) and is deferred until quality observation warrants it.

**Architectural rule going forward:** when adding a new tool, the author owns the size-discipline question. If the tool's worst-case response is unbounded by upstream API design, the tool function must summarize before returning. The size of any tool_result fed to the LLM is the bot's responsibility, not the model's problem to render.

## Consequences

- **Pro:** the project's local-first posture extends to the user-facing layer. No external API key, no per-message cost, no third-party data flow.
- **Pro:** the tool function bodies are runtime-agnostic. If a year from now we replace the bot with a CLI or a web frontend, `bot/tools.py` carries forward unchanged.
- **Pro:** defensive handling for open-source-model failures is in one place (`bot/ollama_client.py`); it's the swappable seam if the LLM strategy changes.
- **Con:** open-source tool calling is less reliable than cloud APIs. Some user queries will misroute. The cap + graceful-failure pattern keeps the bot stable, but the user experience for ambiguous queries is "I had trouble — try rephrasing?" more often than a cloud-LLM bot would emit it.
- **Con:** model load on the Spark. First call after Ollama unloads the model is 20–30s. Subsequent calls under KEEP_ALIVE are <2s. The 120s Ollama timeout absorbs the cold start; users see a "typing…" indicator and don't notice.
- **Con:** the bot is single-tenant. Adding multi-server segregation (different per-server watchlists, per-server allowlists) would be a substantial refactor; out of scope for v1.
- **Con:** allowlist is operator state in `.env`. Adding a user requires editing the file and restarting the bot. Future improvement: a `/allow @user` slash command for the operator. Phase 8+.
