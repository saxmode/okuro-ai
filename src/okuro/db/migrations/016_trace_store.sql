-- <!-- AGENT_HEADER
-- role: code
-- purpose: 016_trace_store module
-- index: content
-- AGENT_HEADER_END -->
-- okuro agent traces — raw execution-trace store for past agent sessions.
--
-- Backs Meta-Harness P1 (full-trace access): future agents can inspect
-- what a prior session actually did (tool calls, errors, decisions)
-- rather than only the compressed summary in memory/progress.
--
-- Provider-agnostic by design: claude-code, codex, gemini, cursor all
-- land in the same tables, distinguished by the `provider` column.
-- Ingesters are per-provider (each CLI stores transcripts differently
-- on disk), but query surface is unified.

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id       TEXT PRIMARY KEY,                   -- native session id from provider
    provider         TEXT NOT NULL,                      -- 'claude-code' | 'codex' | 'gemini' | 'cursor'
    project_path     TEXT,                               -- cwd at session start
    git_branch       TEXT,
    first_ts         TEXT,                               -- ISO8601 timestamp of first event
    last_ts          TEXT,                               -- ISO8601 timestamp of last event
    message_count    INTEGER DEFAULT 0,                  -- total kept events
    user_count       INTEGER DEFAULT 0,
    assistant_count  INTEGER DEFAULT 0,
    tokens_in        INTEGER DEFAULT 0,                  -- sum of prompt tokens
    tokens_out       INTEGER DEFAULT 0,                  -- sum of completion tokens
    model            TEXT,                               -- last observed model id
    source_path      TEXT,                               -- absolute path to source transcript file
    source_mtime     REAL,                               -- float mtime of source file last indexed
    indexed_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_provider ON agent_sessions(provider);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_project  ON agent_sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_last     ON agent_sessions(last_ts DESC);

CREATE TABLE IF NOT EXISTS agent_events (
    uuid            TEXT PRIMARY KEY,                    -- native event uuid from provider
    session_id      TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    parent_uuid     TEXT,                                -- forms DAG within a session
    ord             INTEGER NOT NULL,                    -- line order within source file (0-based)
    type            TEXT NOT NULL,                       -- 'user' | 'assistant' | 'system' | 'progress' | 'tool_result'
    role            TEXT,                                -- 'user' | 'assistant' | 'system' | NULL
    timestamp       TEXT,                                -- ISO8601
    model           TEXT,
    text            TEXT,                                -- flattened plain-text view of content (for FTS)
    content_json    TEXT,                                -- raw structured content preserved verbatim
    tool_name       TEXT,                                -- denormalized for tool-use/tool-result events
    tokens_in       INTEGER,                             -- per-event prompt tokens (assistant only)
    tokens_out      INTEGER                              -- per-event completion tokens (assistant only)
);

CREATE INDEX IF NOT EXISTS idx_agent_events_session    ON agent_events(session_id, ord);
CREATE INDEX IF NOT EXISTS idx_agent_events_type       ON agent_events(type);
CREATE INDEX IF NOT EXISTS idx_agent_events_tool       ON agent_events(tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_events_timestamp  ON agent_events(timestamp);

-- FTS5 index on flattened text. Synced via triggers so we never go stale.
CREATE VIRTUAL TABLE IF NOT EXISTS agent_events_fts USING fts5(
    text,
    session_id UNINDEXED,
    type UNINDEXED,
    content='agent_events',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS agent_events_fts_ai AFTER INSERT ON agent_events
BEGIN
    INSERT INTO agent_events_fts(rowid, text, session_id, type)
    VALUES (new.rowid, new.text, new.session_id, new.type);
END;

CREATE TRIGGER IF NOT EXISTS agent_events_fts_ad AFTER DELETE ON agent_events
BEGIN
    INSERT INTO agent_events_fts(agent_events_fts, rowid, text, session_id, type)
    VALUES ('delete', old.rowid, old.text, old.session_id, old.type);
END;

CREATE TRIGGER IF NOT EXISTS agent_events_fts_au AFTER UPDATE ON agent_events
BEGIN
    INSERT INTO agent_events_fts(agent_events_fts, rowid, text, session_id, type)
    VALUES ('delete', old.rowid, old.text, old.session_id, old.type);
    INSERT INTO agent_events_fts(rowid, text, session_id, type)
    VALUES (new.rowid, new.text, new.session_id, new.type);
END;
