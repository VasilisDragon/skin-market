# ADR 011 — Narrative job: LLM choice, prompt structure, deployment

**Status:** Accepted
**Date:** 2026-05-12
**Related:** ADR 007 (text_value column), ADR 010 (analytics design)

## Context

Phase 5 adds a nightly job that asks a local Ollama model to write a
one-paragraph English summary of the day's notable market moves. The
result is stored as an `insights` row with
`insight_type = 'daily_narrative'`, the prose in `text_value`, and the
structured payload that was passed to the LLM in `meta_info` for
fact-checking.

Three model candidates discussed with the operator:

- `gpt-oss:20b` (OpenAI's open-weights 20B)
- `qwen3-coder`
- The 27b model on the Spark (assumed `gemma3:27b` or similar)

Criteria specified: **good prose + accurate enumeration of stats + no
hallucinated items**.

## Decisions

### 1. Model: `gpt-oss:20b` as default; the 27b model recommended

The implemented default is `gpt-oss:20b` (set in `.env.example` so the
service starts out of the box on any host that has it). The operator
overrides via `OLLAMA_MODEL` once they confirm latency on the 27b is
acceptable and prose quality is meaningfully better.

Rationale:

- **`gpt-oss:20b`** — 20B-parameter general-purpose model from
  OpenAI's open weights release. Best balance of capability and speed
  at this size class. Reasonable prose, reasonable instruction
  following, reasonable hallucination resistance for a well-structured
  prompt. Solid default for a once-daily batch job.
- **`qwen3-coder`** — coding fine-tunes are typically *worse* at
  open-ended narrative prose than their general counterparts (over-
  weighted toward code-tokens, lower natural-language fluency).
  Wrong tool for this job.
- **The 27b model** — theoretically best on all three criteria
  (more parameters → better prose, better arithmetic, less hallucination).
  Latency is acceptable for a 02:00-UTC job. The reason it isn't the
  default is operational: I don't know its exact model name on the
  Spark, and hardcoding an Ollama tag I'd be guessing at would break
  the first deploy. The env var makes the swap one config change.

Operator note: after the first successful narrative on `gpt-oss:20b`,
flip `OLLAMA_MODEL` to the actual 27b tag in `.env`, restart the
analytics container, compare quality. The narrative-job code is
model-agnostic; any Ollama-compatible chat model works.

### 2. Prompt structure: explicit anti-hallucination guardrails

The system prompt makes three things explicit:

1. *Use only items in the data block.* The model is not allowed to
   reach for prior knowledge about other CS2 items.
2. *Use the exact item names from the data.* Abbreviations and
   renames are forbidden.
3. *Every price must include its denomination tag.* "$42 USD" and
   "$42 wallet credit" are different facts.

The user prompt is a JSON block: `top_movers`, `volume_anomalies`,
`cross_source_divergences`. Tight cap on item count (`TOP_N_MOVERS=8`)
keeps the prompt small — smaller prompts give better
instruction-following on most models, and the token budget isn't the
bottleneck for nightly batch.

`temperature=0.4` — low but not zero. Pure greedy (`0.0`) tends to
produce mechanical, list-like prose; mild temperature gives sentence
variety without inviting drift.

### 3. Citation invariant: `meta_info` carries the full input payload

The structured payload the model received is stored verbatim in the
inserted `insights` row's `meta_info`. A reviewer can compare the
prose in `text_value` against the data and verify every claim.
Hallucinated facts (item names, percentages, sources not in the data)
are detectable by this comparison.

This is intentional: the LLM is doing prose generation, not data
synthesis. The "truth" is the data; the prose is a presentation
layer. The audit trail makes that explicit.

### 4. Failure modes: silent skip, not stale row

If Ollama is unreachable, returns an error, or returns empty text,
the job logs ERROR and inserts nothing. The next night's run tries
again. The bot's reply path falls back to raw numbers when no recent
`daily_narrative` row exists; it does not surface a stale narrative.

### 5. Deployment: separate analytics service, not in the collector

The analytics scheduler runs as its own Docker service
(`skinmarket-analytics`), using the same image as
`skinmarket-collector` with a different `command:` override. Reasons:

- **Clean responsibility boundary** — collector writes prices,
  analytics reads them. Separate restart cycles when one needs
  attention.
- **No resource contention** — the daily narrative LLM call can run
  for minutes on a 27b model. Mixing it with the collector's HTTP
  pool to Steam/Skinport would be a code smell at best.
- **Ollama access** — the analytics container needs `extra_hosts:
  ["host.docker.internal:host-gateway"]` to reach the host's
  Ollama. Adding this to the collector container would be
  meaningless permission widening.

Both services share the same Dockerfile (and the same `uv.lock`),
so build time isn't duplicated.

## Consequences

- **Pro:** the LLM choice is a config change, not a code change.
  Swapping to a 70b model on a different host requires only an env
  var update.
- **Pro:** every narrative is independently fact-checkable. The
  audit trail in `meta_info` means a reviewer can run "did the
  prose match the data" pairs against an LLM critic later if we
  want automated quality monitoring.
- **Pro:** failure modes are quiet. A bad night doesn't produce
  bad data downstream — the analytics row simply isn't there.
- **Con:** `gpt-oss:20b` may need to be installed on the Spark
  manually if it isn't there. Documented in `docs/operations.md`
  (next update) as a one-line `ollama pull` step.
- **Con:** the model writes prose; the prose is one English
  paragraph; subtle stylistic regressions across Ollama model
  updates are hard to detect without periodic human review. No
  automated quality check in v1; flagged as v2+ work.
- **Related:** the bot's reply path (Phase 7) treats the
  `daily_narrative` row as supplementary context. Raw prices and
  per-item insights are the primary signal; narrative is the
  "anything interesting happen today" answer.
