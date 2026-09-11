-- <!-- AGENT_HEADER
-- role: code
-- purpose: 024_surface_log_unified module
-- index: content
-- AGENT_HEADER_END -->
-- okuro unified surface-log — single substrate for memory + thought surfacing.
-- Supersedes memory_surface_log (018). Thoughts get objective-utility signal
-- equivalent to what memories already had — see Meta-Harness P8.

CREATE TABLE IF NOT EXISTS surface_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('memory','thought')),
    entity_id    TEXT NOT NULL,
    session_id   TEXT,
    context      TEXT NOT NULL,
    surfaced_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_surface_log_kind_entity ON surface_log(kind, entity_id);
CREATE INDEX IF NOT EXISTS idx_surface_log_session     ON surface_log(session_id);
CREATE INDEX IF NOT EXISTS idx_surface_log_at          ON surface_log(surfaced_at DESC);

-- Backfill from memory_surface_log, then drop the old table.
INSERT INTO surface_log (kind, entity_id, session_id, context, surfaced_at)
SELECT 'memory', memory_id, session_id, context, surfaced_at FROM memory_surface_log;

DROP TABLE memory_surface_log;
