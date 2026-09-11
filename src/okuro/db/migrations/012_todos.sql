-- 012_todos.sql
-- Todos — actionable items registry (distinct from thoughts/memories/reminders).
--
-- Schema captures the pre-existing live `todos` table so its shape is
-- versioned in-repo. Agents reach for this via the `todo_*` MCP tools
-- (sense/todos.py) when they discover an actionable item that needs
-- the user's attention later — not a persistent learning (write_memory),
-- not an ephemeral idea (capture_thought), not a time-scheduled nudge
-- (set_reminder).

CREATE TABLE IF NOT EXISTS todos (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    detail          TEXT,
    status          TEXT DEFAULT 'open'
                    CHECK (status IN ('open','doing','done','dropped')),
    priority        INTEGER DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    project         TEXT,
    due_at          TEXT,
    reminder_id     TEXT REFERENCES reminders(id) ON DELETE SET NULL,
    source          TEXT DEFAULT 'user'
                    CHECK (source IN ('user','agent','system','ingress')),
    source_event_id TEXT,
    context         TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_todos_status
    ON todos(status, priority DESC, due_at);
CREATE INDEX IF NOT EXISTS idx_todos_project
    ON todos(project);
CREATE INDEX IF NOT EXISTS idx_todos_source_event
    ON todos(source_event_id) WHERE source_event_id IS NOT NULL;
