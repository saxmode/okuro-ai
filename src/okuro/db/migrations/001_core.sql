-- <!-- AGENT_HEADER
-- role: code
-- purpose: 001_core module
-- index: content
-- AGENT_HEADER_END -->
-- okuro core tables: profile, projects, principles, memory, thoughts, progress, persons

CREATE TABLE IF NOT EXISTS user_profile (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    profile    TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    path        TEXT,
    url         TEXT,
    stack       TEXT DEFAULT '[]',
    port_range  TEXT,
    roles       TEXT DEFAULT '[]',
    description TEXT,
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS principles (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT,
    source      TEXT,
    examples    TEXT DEFAULT '[]',
    priority    INTEGER DEFAULT 50,
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_memory (
    id              TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    content         TEXT NOT NULL,
    project         TEXT REFERENCES projects(id),
    role            TEXT,
    confidence      REAL DEFAULT 1.0,
    source_agent    TEXT,
    supersedes      TEXT REFERENCES agent_memory(id),
    verified        INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    last_accessed   TEXT
);

-- vec_memory created post-migrate by ensure_vec_dims() at active tier dim.

CREATE TABLE IF NOT EXISTS thoughts (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    metadata        TEXT DEFAULT '{}',
    status          TEXT DEFAULT 'open' CHECK (status IN ('open','resolved','deferred','dismissed')),
    project         TEXT REFERENCES projects(id),
    last_surfaced   TEXT,
    surface_count   INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- vec_thoughts created post-migrate by ensure_vec_dims() at active tier dim.

CREATE TABLE IF NOT EXISTS progress (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL REFERENCES projects(id),
    agent           TEXT NOT NULL,
    status          TEXT CHECK (status IN ('exploring','implementing','testing','blocked','waiting_for_user')),
    summary         TEXT,
    files_touched   TEXT DEFAULT '[]',
    next_steps      TEXT,
    blockers        TEXT,
    branch          TEXT,
    history         TEXT DEFAULT '[]',
    started_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_progress_project_agent ON progress(project, agent);

CREATE TABLE IF NOT EXISTS persons (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    organization    TEXT,
    role            TEXT,
    relation_to_user TEXT,
    relation_type   TEXT CHECK (relation_type IN ('colleague','friend','family','professional','acquaintance','other')),
    communication   TEXT DEFAULT '{}',
    cognitive       TEXT DEFAULT '{}',
    notes           TEXT,
    contact         TEXT DEFAULT '{}',
    tags            TEXT DEFAULT '[]',
    active          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- vec_persons created post-migrate by ensure_vec_dims() at active tier dim.
