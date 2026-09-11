-- <!-- AGENT_HEADER
-- role: code
-- purpose: 020_agent_session_sources module
-- index: content
-- AGENT_HEADER_END -->
-- Allow a single agent_sessions row to be built up from multiple transcript
-- files (Claude Code sometimes reuses a session_id across project dirs,
-- which previously collided under the ingester's ON CONFLICT → last-wins
-- and silently dropped ~18 out of 2905 sessions).
--
-- The new agent_session_sources table is 1:N from sessions. The ingester
-- upserts one row per source_path and re-aggregates stats on
-- agent_sessions from agent_events after each file.
--
-- agent_sessions.source_path / source_mtime are kept for backward
-- compatibility; we populate them with whichever source was ingested
-- most recently so existing callers keep working.

CREATE TABLE IF NOT EXISTS agent_session_sources (
    source_path   TEXT PRIMARY KEY,                                -- absolute path to the transcript file
    session_id    TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    source_mtime  REAL,
    indexed_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_session_sources_session
    ON agent_session_sources(session_id);

-- Backfill from the current single-source columns on agent_sessions.
INSERT OR IGNORE INTO agent_session_sources (source_path, session_id, source_mtime, indexed_at)
SELECT source_path, session_id, source_mtime, indexed_at
FROM   agent_sessions
WHERE  source_path IS NOT NULL;
