# Archived: Phase 7b Hermes skill attempt

Preserved for reference; **not the working implementation**.

The working Discord bot lives at `bot/` in the repo root (Phase 7c).
It uses discord.py + a local Ollama daemon for tool-call routing
instead of the Hermes plugin pattern this directory scaffolded for.

## Why this is archived rather than deleted

The `tools.py` function bodies and `SKILL.md` rendering rules in
this directory are the high-value content that carried forward to
the working implementation:

- `tools.py` → `bot/tools.py` (same function bodies + typed
  exception hierarchy + three-state composer; only the
  registration format changed from `@tool` decorator + `TOOLS`
  list to `TOOL_DEFINITIONS` + `TOOL_FUNCTIONS`).
- `SKILL.md` → split into `bot/system_prompt.py` (LLM-facing
  routing) and `bot/README.md` (operator-facing).

Diffing those files against the new versions shows what was kept
and what was changed — useful when revisiting the rationale.

ADR 015 (still in place at `docs/adr/015-bot-skill-design.md`)
documents the original design decisions; ADR 016 documents how the
runtime ended up different.
