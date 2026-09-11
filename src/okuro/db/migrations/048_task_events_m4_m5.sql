-- <!-- AGENT_HEADER
-- role: code
-- purpose: 048_task_events_m4_m5 — extend task_events.event_type CHECK
--   to admit M4 (role_slice, role_body_fetched) + M5 (bootstrap_sizes)
--   telemetry event_types. Same rebuild dance as 046 (verdict).
-- index: content
-- AGENT_HEADER_END -->
--
-- Backfills the constraint that M4 and M5 forgot to widen. Without this
-- migration the dispatcher and bootstrap assembler swallow IntegrityError
-- on every emit (the call sites are wrapped in try/except), so M4
-- role_slice + role_body_fetched telemetry never actually persisted —
-- success-check accounting was unfalsifiable.
--
-- Why one migration for both milestones:
--   The widening is a single CHECK rewrite; splitting into 047+048
--   would force two table rebuilds for no isolation gain.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS task_events_v3 (
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
                        'bootstrap_sizes'
                    )),
    body            TEXT NOT NULL DEFAULT '{}',
    supersedes      TEXT REFERENCES task_events_v3(id),
    confidence      REAL DEFAULT 0.8,
    seq             INTEGER NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    created_by      TEXT,
    UNIQUE (task_id, seq)
);

INSERT OR IGNORE INTO task_events_v3 (
    id, task_id, subtask_id, from_role, event_type,
    body, supersedes, confidence, seq, created_at, created_by
)
SELECT
    id, task_id, subtask_id, from_role, event_type,
    body, supersedes, confidence, seq, created_at, created_by
FROM task_events;

DROP TABLE IF EXISTS task_events;
ALTER TABLE task_events_v3 RENAME TO task_events;

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
