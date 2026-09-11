-- <!-- AGENT_HEADER
-- role: code
-- purpose: 006_provider_compliance module
-- index: content
-- AGENT_HEADER_END -->
-- Provider compliance aggregation table

CREATE TABLE IF NOT EXISTS provider_compliance (
    provider            TEXT PRIMARY KEY,
    total_sessions      INTEGER DEFAULT 0,
    scored_sessions     INTEGER DEFAULT 0,
    avg_score           REAL DEFAULT 0,
    avg_normalized      REAL DEFAULT 0,
    bootstrap_rate      REAL DEFAULT 0,
    report_rate         REAL DEFAULT 0,
    cortex_rate         REAL DEFAULT 0,
    memory_rate         REAL DEFAULT 0,
    progress_rate       REAL DEFAULT 0,
    last_updated        TEXT DEFAULT (datetime('now')),
    window_days         INTEGER DEFAULT 30
);
