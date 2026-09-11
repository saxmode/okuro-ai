-- <!-- AGENT_HEADER
-- role: code
-- purpose: 055_task_events_convergence_telemetry — widen task_events.event_type
--   CHECK to admit 'convergence_telemetry' (PR 4 session-loop retry telemetry).
-- index: content
-- AGENT_HEADER_END -->
--
-- PR 4 added 'convergence_telemetry' to the canonical EventType list
-- (sense/task_events.py) and the dispatcher emits one row per session-loop
-- review publish (dispatcher_streaming.py), but the DB CHECK constraint was
-- never widened to match — so every emit raised IntegrityError (swallowed by
-- the caller, telemetry silently dropped). This syncs the DB allow-list to the
-- code's canonical EventType. Same rebuild dance as migrations 046 / 048 / 050.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS task_events_v5 (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    subtask_id      TEXT NOT NULL,
    from_role       TEXT,
    event_type      TEXT NOT NULL
                    CHECK (event_type IN (
                        'decision', 'supersedes', 'contract',
                        'gap', 'open_question', 'compression',
                        'verdict',
                        'role_slice', 'role_body_fetched',
                        'bootstrap_sizes', 'spawn_usage',
                        'convergence_telemetry'
                    )),
    body            TEXT NOT NULL DEFAULT '{}',
    supersedes      TEXT REFERENCES task_events_v5(id),
    confidence      REAL DEFAULT 0.8,
    seq             INTEGER NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    created_by      TEXT,
    UNIQUE (task_id, seq)
);

INSERT OR IGNORE INTO task_events_v5 (
    id, task_id, subtask_id, from_role, event_type,
    body, supersedes, confidence, seq, created_at, created_by
)
SELECT
    id, task_id, subtask_id, from_role, event_type,
    body, supersedes, confidence, seq, created_at, created_by
FROM task_events;

DROP TABLE IF EXISTS task_events;
ALTER TABLE task_events_v5 RENAME TO task_events;

CREATE INDEX IF NOT EXISTS idx_task_events_task        ON task_events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_task_events_subtask     ON task_events(subtask_id);
CREATE INDEX IF NOT EXISTS idx_task_events_type        ON task_events(task_id, event_type, seq);
CREATE INDEX IF NOT EXISTS idx_task_events_supersedes  ON task_events(supersedes);

CREATE TRIGGER IF NOT EXISTS task_events_reject_empty_id_ins
BEFORE INSERT ON task_events
FOR EACH ROW
WHEN NEW.id IS NULL OR TRIM(NEW.id) = ''
BEGIN
    SELECT RAISE(ABORT, 'task_events.id must be a non-empty string');
END;

CREATE TRIGGER IF NOT EXISTS task_events_reject_empty_task_ins
BEFORE INSERT ON task_events
FOR EACH ROW
WHEN NEW.task_id IS NULL OR TRIM(NEW.task_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'task_events.task_id must be a non-empty string');
END;

PRAGMA foreign_keys = ON;
