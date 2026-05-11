# ADR 001 — Use `uv` for Python dependency management

**Status:** Accepted
**Date:** 2026-05-11

## Context

The project needs a Python dependency manager that produces a reproducible
lockfile, supports a single-command "set up the dev environment" flow on a
fresh machine, and stays out of the way of someone debugging at 2am. ARCHITECTURE.md
explicitly calls for "boring, well-documented tools" — but it also calls for
dependency locking, which `pip install -r requirements.txt` does not provide.

## Decision

Use `uv` (Astral) for dependency management. Dependencies are declared in
`pyproject.toml` under `[project]` (runtime) and `[dependency-groups]` (dev,
PEP 735). `uv.lock` is committed to the repo as the source of truth for
reproducible installs. `uv sync` reconstructs the virtualenv from the lock.

## Alternatives considered

- **pip + requirements.txt**: no lockfile semantics, no dev/runtime
  separation, no environment management. Rejected as too primitive for
  reproducibility goals.
- **pip-tools (`pip-compile`)**: gives a lockfile but no virtualenv
  management; needs glue to be a complete workflow.
- **Poetry**: well-known and full-featured, but slower, more opinionated
  about project layout, and has a history of friction with PEP-standard
  metadata. Rejected on speed and standards-compliance grounds.
- **Hatch / PDM**: viable but smaller communities than uv right now.
- **Conda / Mamba**: overkill for a project with no native-binary scientific
  dependencies beyond `psycopg[binary]` and `matplotlib`, both of which
  ship wheels.

## Consequences

- **Pro:** sub-second resolves, single CLI for venv + deps + run, native
  support for PEP 621/735 standards, easy to swap for plain pip later if
  needed (the `[project]` metadata stays valid).
- **Con:** users need `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
  Documented in README.md.
- **Con:** `uv` is still pre-1.0; behavior between minor versions may shift.
  Mitigation: pin the `uv` version in CI when CI exists.
- This project is configured as a non-package application
  (`[tool.uv] package = false`); modules are imported relative to the project
  root via `uv run`.
