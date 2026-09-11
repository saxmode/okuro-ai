-- <!-- AGENT_HEADER
-- role: code
-- purpose: 003_signals module
-- index: content
-- AGENT_HEADER_END -->
-- okuro signal tables (ported from the predecessor system)

CREATE TABLE IF NOT EXISTS signal_dismissals (
    signal_id    TEXT PRIMARY KEY,
    dismissed_at TEXT DEFAULT (datetime('now')),
    reason       TEXT
);
