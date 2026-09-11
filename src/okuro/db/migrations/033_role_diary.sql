-- <!-- AGENT_HEADER
-- role: code
-- purpose: 033_role_diary — per-role/per-agent diary streams (mempalace agent-diary pattern).
-- index: content
-- AGENT_HEADER_END -->
--
-- Distinct from role_knowledge (curated insights, dedup, type-checked).
-- Diary entries are chronological narrative — what happened in this session,
-- what the role observed, recurring patterns. Low-curation, append-only.
--
-- Both role_id and agent are nullable so callers can write diaries against
-- a formal role, a free-form agent label ("reviewer", "research-bot"), or
-- both. Diary entries can later crystallize into role_knowledge via
-- existing roles_learn().

CREATE TABLE IF NOT EXISTS role_diary_entries (
    id          TEXT PRIMARY KEY,
    role_id     TEXT REFERENCES roles(role_id) ON DELETE CASCADE,
    agent       TEXT,
    project     TEXT REFERENCES projects(id),
    topic       TEXT,
    entry       TEXT NOT NULL,
    session_id  TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_role_diary_role_ts    ON role_diary_entries(role_id, created_at);
CREATE INDEX IF NOT EXISTS idx_role_diary_agent_ts   ON role_diary_entries(agent, created_at);
CREATE INDEX IF NOT EXISTS idx_role_diary_project    ON role_diary_entries(project);
CREATE INDEX IF NOT EXISTS idx_role_diary_session_id ON role_diary_entries(session_id);

-- vec_role_diary created post-migrate by ensure_vec_dims() at active tier dim.
