# ADR 026 — DeepSeek inference hard cutover and usage accounting

**Status:** Accepted
**Date:** 2026-05-23
**Related:** ADR 011 (narrative job), ADR 016 (Discord bot runtime),
ADR 017 (freshness split), ADR 024 (tier architecture).

## Context

The project originally used local Ollama for both LLM call sites:
Discord bot tool-calling / response phrasing and the nightly narrative
job. The operator requested a hard cutover to DeepSeek API inference,
not a stack relocation. Collectors, analytics math, drift detection,
Postgres, and the read API stay on the homelab stack; only the two LLM
call sites move.

Before changing inference code, we added
`tests/test_bot.py::TestToolCallingRegressionFixture`: representative
Discord queries for price lookup, drift, history, watchlist listing,
wear ambiguity, and not-tracked items. It fixes the expected tool
selection and shaped tool result the LLM sees. That fixture is the
quality floor for prompt and backend changes.

## Decision

Use DeepSeek's OpenAI-format chat API directly through `httpx`:

- Base URL: `https://api.deepseek.com`
- Model: `deepseek-v4-flash`
- Mode: non-thinking, via `{"thinking": {"type": "disabled"}}`
- Temperature: `0` for bot tool-routing turns; `0.2` for the nightly
  narrative prose path.

DeepSeek's official pricing page currently lists `deepseek-v4-flash`
and `deepseek-v4-pro`, marks tool calls as supported on both, and says
the legacy `deepseek-chat` / `deepseek-reasoner` names map to flash
non-thinking / thinking modes but will be deprecated. The same page
lists `deepseek-v4-flash` prices per 1M tokens as:

- input cache hit: `$0.0028`
- input cache miss: `$0.14`
- output: `$0.28`

Verified source: <https://api-docs.deepseek.com/quick_start/pricing/>
(lines 47-65 as of this ADR).

The chat reference documents `deepseek-v4-flash` as an accepted model
ID and `thinking.type = disabled` as non-thinking mode. Verified
source: <https://api-docs.deepseek.com/api/create-chat-completion/>
(model and thinking fields).

DeepSeek context caching is enabled by default and returns
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` in the `usage`
block. We rely on that automatic prefix caching for the stable system
prompt + tool-definition prefix; no explicit cache API is needed.
Verified source: <https://api-docs.deepseek.com/guides/kv_cache/>.

## Usage Accounting

Migration `0011` adds `llm_usage_log`. One row is inserted per
DeepSeek request from either LLM site. Rows store:

- request UUID, timestamp, nullable Discord user ID
- model string
- prompt, completion, total, cache-hit, and cache-miss token counts
- DeepSeek prices in effect at request time
- computed USD cost using those stored rates
- `prompt_sha256` and a bounded redacted `prompt_preview`
- nullable `full_prompt`, populated only when
  `LLM_USAGE_LOG_FULL_PROMPT=true`
- raw `usage` JSON for audit

Cost formula for `deepseek-v4-flash`:

```
(cache_hit_tokens * 0.0028
 + cache_miss_tokens * 0.14
 + completion_tokens * 0.28) / 1_000_000
```

The Discord bot now has `DATABASE_URL` solely for this write-only usage
accounting path. Market data reads still go through the FastAPI API;
the bot does not read Postgres for market facts.

## Prompt Optimization

The bot system prompt was rewritten from explanatory prose into direct
routing and rendering directives. Size dropped from about 12.8 KB to
about 5.0 KB while preserving the regression-pinned requirements:
`query_drift`, wear swaps, correctly-priced clarification, and drift
before anomaly render order. The DeepSeek context cache should further
discount repeated stable-prefix tokens when cache hits occur.

## Rejected

- **DeepSeek Pro / thinking mode.** Rejected for the bot path because
  the requirement is fast, cheap tool selection and concise phrasing.
  Thinking mode adds reasoning tokens and requires reasoning-content
  handling across tool turns. Non-thinking flash supports tool calls and
  is cheaper.
- **OpenAI SDK dependency.** Rejected because `httpx` is already a
  project-standard dependency and the needed API surface is a single
  POST. Avoiding another SDK keeps the operator's debug surface smaller.
- **Ollama fallback.** Rejected by the hard-cutover requirement.
  Missing DeepSeek configuration fails fast; it does not silently route
  to Ollama.

## Consequences

- Inference now depends on DeepSeek network/API availability and key
  configuration.
- Every successful DeepSeek response must include a `usage` block; a
  missing block is treated as an error because cost accounting would be
  incomplete.
- The old local-first privacy posture no longer applies to addressed
  Discord messages or narrative prompts. Non-addressed Discord messages
  are still ignored and never logged.
- Pricing can change. Future price changes must update the constants in
  `db/llm_usage.py` and a new ADR note, while old rows retain the rates
  that were in effect when written.
