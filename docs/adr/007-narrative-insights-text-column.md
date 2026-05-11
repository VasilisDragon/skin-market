# ADR 007 — `insights.text_value TEXT` column for narrative insights

**Status:** Accepted
**Date:** 2026-05-11

Phase 5's `daily_narrative` insight type needs to store an English
paragraph alongside a row in `insights`. The existing schema offers
`value NUMERIC` (wrong type) or `meta_info JSONB` (works, but text in
JSONB hurts full-text search, indexing, and casual `SELECT` readability).

**Decision:** add a dedicated `text_value TEXT` column to `insights` in
the Phase 5 migration. NUMERIC `value` stays for numeric insights (MAs,
anomaly scores). `meta_info` holds the per-narrative provenance JSON
(which items + price moves the LLM cited). One small migration; the
data stays queryable with plain SQL.
