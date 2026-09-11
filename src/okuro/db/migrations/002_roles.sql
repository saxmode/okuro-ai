-- <!-- AGENT_HEADER
-- role: code
-- purpose: 002_roles module
-- index: content
-- AGENT_HEADER_END -->
-- okuro roles tables (from tm-eichi)

CREATE TABLE IF NOT EXISTS roles (
    role_id             TEXT PRIMARY KEY,
    domain              TEXT NOT NULL,
    description         TEXT,
    maturity            TEXT DEFAULT 'draft',
    tier                TEXT DEFAULT 'standard',
    model               TEXT DEFAULT 'sonnet',
    sessions            INTEGER DEFAULT 0,
    learnings           INTEGER DEFAULT 0,
    maintenance_schedule TEXT,
    last_maintained     TEXT,
    prompt              TEXT,
    lean_prompt         TEXT,
    micro_prompt        TEXT,
    tools               TEXT DEFAULT '[]',
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- vec_roles created post-migrate by ensure_vec_dims() at active tier dim.

CREATE TABLE IF NOT EXISTS role_knowledge (
    id          TEXT PRIMARY KEY,
    role_id     TEXT NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    type        TEXT CHECK (type IN ('insight','pitfall','source','decision','research')),
    source_file TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- vec_knowledge created post-migrate by ensure_vec_dims() at active tier dim.
