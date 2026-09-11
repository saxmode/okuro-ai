-- <!-- AGENT_HEADER
-- role: code
-- purpose: 030_transcripts — verbatim per-message transcript ingest (mempalace sweeper port).
-- index: content
-- AGENT_HEADER_END -->
--
-- Provider-agnostic transcript store. One row per user/assistant/system message
-- across Claude/Gemini/Codex/Cursor sessions. Idempotent ingest via deterministic
-- ID = sha256(source || ':' || session_id || ':' || message_uuid). Resume-safe:
-- re-running sweep on a partially-ingested transcript is a no-op for already-
-- written rows.
--
-- Companion vec0 table for semantic search via sqlite-vec.

CREATE TABLE IF NOT EXISTS transcript_messages (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    message_uuid    TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    ts              TEXT,
    project         TEXT REFERENCES projects(id),
    transcript_path TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transcript_session    ON transcript_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_transcript_source_ts  ON transcript_messages(source, ts);
CREATE INDEX IF NOT EXISTS idx_transcript_project    ON transcript_messages(project);
CREATE INDEX IF NOT EXISTS idx_transcript_role       ON transcript_messages(role);

-- vec_transcript_messages created post-migrate by ensure_vec_dims() at active tier dim.
