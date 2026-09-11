-- <!-- AGENT_HEADER
-- role: code
-- purpose: 004_telemetry module
-- index: content
-- AGENT_HEADER_END -->
-- okuro telemetry tables (sessions, tool usage, feedback)

CREATE TABLE IF NOT EXISTS sessions (
    id                   TEXT PRIMARY KEY,
    session_id           TEXT UNIQUE NOT NULL,
    provider             TEXT,
    task_hint            TEXT,
    project              TEXT REFERENCES projects(id),
    session_type         TEXT,
    started_at           TEXT DEFAULT (datetime('now')),
    ended_at             TEXT,
    compliance_score     INTEGER,
    compliance_normalized REAL,
    tools_used           TEXT DEFAULT '[]',
    tools_expected       TEXT DEFAULT '[]',
    end_reason           TEXT,
    metadata             TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sessions(provider);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

CREATE TABLE IF NOT EXISTS tool_usage (
    id          TEXT PRIMARY KEY,
    session_id  TEXT REFERENCES sessions(session_id),
    server      TEXT,
    tool        TEXT NOT NULL,
    called_at   TEXT DEFAULT (datetime('now')),
    latency_ms  INTEGER,
    ok          INTEGER DEFAULT 1,
    arg_keys    TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_tool_usage_session ON tool_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_usage_tool ON tool_usage(tool);

CREATE TABLE IF NOT EXISTS tool_feedback (
    id              TEXT PRIMARY KEY,
    session_id      TEXT REFERENCES sessions(session_id),
    tool_name       TEXT NOT NULL,
    used            INTEGER,
    useful          INTEGER,
    comment         TEXT,
    bypass_reason   TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);
