-- Migration 058 — cortex query log (audit F38/F39).
--
-- cortex_search telemetry stored only arg_keys (the NAMES of the arguments),
-- never the query VALUE, the result count, or whether the query returned
-- nothing. So there was no way to answer "what are users searching for?",
-- "what is the zero-result rate?", or "how slow are searches?" — the system
-- was flying blind on retrieval quality (DP07 observe-not-ask).
--
-- STORAGE CHOICE — a dedicated table, NOT the usage.jsonl telemetry file.
-- Rationale: the aggregates we need (zero-result-rate, search volume, p50/p95
-- latency) are SQL GROUP BY queries; computing them by parsing an append-only
-- JSONL file every time the stats endpoint is hit is wasteful and fragile. A
-- table makes 5.2/5.3 a one-liner aggregate, is install-portable, and the
-- write is best-effort/fire-and-forget so it never adds hot-path latency.
-- This is a single-user local system, so storing the raw query text is fine
-- and useful (it's the operator's own searches).
--
-- Rows are append-only; a periodic prune can cap the table if it ever grows
-- (search volume on a personal install is low). Indexed by created_at for the
-- time-windowed aggregates the stats endpoint runs.

CREATE TABLE IF NOT EXISTS cortex_query_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    query          TEXT NOT NULL,
    project        TEXT,                 -- scope slug, or NULL for unscoped
    n_results      INTEGER NOT NULL DEFAULT 0,
    zero_result    INTEGER NOT NULL DEFAULT 0,  -- 1 if n_results == 0
    latency_ms     REAL,
    hybrid         INTEGER NOT NULL DEFAULT 0,   -- 1 if the hybrid path ran
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cortex_query_log_created
    ON cortex_query_log(created_at);
CREATE INDEX IF NOT EXISTS idx_cortex_query_log_zero
    ON cortex_query_log(zero_result, created_at);
