# ADR 027: DeepSeek Context Cache And Cost Control

Date: 2026-05-23

Status: Accepted

## Context

ADR 026 moved bot inference to DeepSeek. The follow-up Phase 1 gate required
verification of current pricing, prompt/context caching behavior, measured
per-query costs, and live response quality.

The bot sends a stable prefix on every DeepSeek turn:

- `bot/system_prompt.py`
- `bot.tools.TOOL_DEFINITIONS`

The baseline prefix was larger than needed and included repeated routing prose
in both places. It also allowed two quality failures during live spot checks:

- 404/not-tracked responses could add unsupported item lore.
- Omitted-wear questions could trigger multiple item tools instead of a
  clarification.

## Decision

Use DeepSeek's automatic context caching. Do not add an application-managed
prompt cache layer.

Official DeepSeek docs state that context caching is enabled by default and
that cache status is reported through `prompt_cache_hit_tokens` and
`prompt_cache_miss_tokens` in the response usage block:

- https://api-docs.deepseek.com/guides/kv_cache/

Continue recording these fields in `llm_usage_log`; that table is the source of
truth for cost analysis.

The 2026-05-23 paid-bot follow-up adds optional rolling budget guards backed by
that same table:

- `DEEPSEEK_DAILY_COST_LIMIT_USD`: global 24-hour spend ceiling.
- `DEEPSEEK_DAILY_USER_COST_LIMIT_USD`: per-Discord-user 24-hour spend ceiling.

When either configured limit has already been reached, the bot returns a
deterministic budget-exhausted reply before making another DeepSeek request.
Unset or non-positive limits disable the guard.

Trim the stable prefix instead of adding new runtime machinery:

- Rewrite `SYSTEM_PROMPT` into shorter, directive-style routing and rendering
  rules.
- Keep anti-hallucination, denomination, tier, and error-scope rules explicit.
- Shorten OpenAI-compatible tool descriptions in `TOOL_DEFINITIONS`.
- Keep tool order and request shape stable so DeepSeek can reuse common
  prefixes on a best-effort basis.

Pricing remains the values verified from the official pricing page on
2026-05-23:

- https://api-docs.deepseek.com/quick_start/pricing/
- `deepseek-v4-flash`
- Cache hit input: `$0.0028 / 1M tokens`
- Cache miss input: `$0.14 / 1M tokens`
- Output: `$0.28 / 1M tokens`

## Consequences

The prompt/tool prefix is materially smaller:

- `SYSTEM_PROMPT`: `5,621 -> 4,733` chars
- `TOOL_DEFINITIONS` JSON: `4,901 -> 3,048` chars
- Combined stable prefix: `10,522 -> 7,781` chars

The final live representative run in `llm_usage_log` shows:

- Prompt tokens: `72,827 -> 46,524`
- Total tokens: `75,650 -> 48,188`
- Cost: `$0.00215266 -> $0.00130683`

The regular regression fixture still passes:

- `tests/test_bot.py::TestToolCallingRegressionFixture`
- `tests/test_bot.py::TestSystemPrompt`

The full pre-commit verification remains:

- `uv run ruff check .`
- `uv run pytest`

## Alternatives Considered

### Explicit Prompt Cache API

Rejected. DeepSeek's documented cache is automatic and best-effort. There is no
cache-key or cache-handle API for this chat path to integrate.

### Bigger Prompt With More Examples

Rejected for this gate. Live samples showed that shorter, constraining
directives reduced both cost and tool overuse. More examples would increase
the stable prefix and could reduce cache/miss efficiency.

### Tool-Side Wear Resolver

Deferred. A deterministic wear resolver would be preferable to asking the LLM
to infer active wears from a summarized 5,000-item watchlist. For this gate,
the safer behavior is to ask a clarification when wear is omitted.
