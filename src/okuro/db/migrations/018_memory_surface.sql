-- <!-- AGENT_HEADER
-- role: code
-- purpose: 018_memory_surface module
-- index: content
-- AGENT_HEADER_END -->
-- okuro memory-surface log — objective memory-utility signal (Meta-Harness P8).
--
-- Every time a memory is shown to an agent (via read_memory or via
-- bootstrap's Memories section) we log a row here. Joining this log
-- against ``sessions.compliance_normalized`` gives an external, non-
-- self-rated signal for whether surfacing a given memory correlates
-- with better or worse session outcomes.
--
-- Stats are computed on-demand by src/okuro/sense/memory_utility.py
-- (no separate aggregation table — cheap enough at the scale we expect
-- over the first few weeks).

CREATE TABLE IF NOT EXISTS memory_surface_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id    TEXT NOT NULL,
    session_id   TEXT,                              -- okuro session_id; NULL if no active session at surface time
    context      TEXT NOT NULL,                     -- 'read_memory' | 'bootstrap_system' | 'bootstrap_relevant'
    surfaced_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_surface_memory  ON memory_surface_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_surface_session ON memory_surface_log(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_surface_at      ON memory_surface_log(surfaced_at DESC);
