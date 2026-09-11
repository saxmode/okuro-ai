-- <!-- AGENT_HEADER
-- role: code
-- purpose: 046_task_events_verdict — extend M2 task_events.event_type
--   CHECK constraint to admit 'verdict' (M3 reviewer-pipeline output).
--   Append-only contract preserved — no rows mutated, no existing
--   event_type changed; only the CHECK is widened. SQLite has no
--   ALTER TABLE ... DROP CONSTRAINT, so we rebuild the table via the
--   standard rename+copy dance.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a new event_type, not a flag on existing types:
--   The M2 contract is "one typed event per row, ordered by seq,
--   conflict-detected at append". Verdicts have a different body shape
--   than decisions / supersedes / contracts and are emitted by a
--   different actor (the reviewer pipeline, not a subagent). Tagging
--   would fold two semantics into one type and break conflict-detect
--   downstream. Adding the type keeps the projections clean.
--
-- Body shape (validated in okuro.sense.task_events.VerdictBody):
--   {
--     "phase_id": <int>,
--     "verdict":  "PASS" | "CONDITIONAL" | "FAIL",
--     "deterministic_failed":         <int>,
--     "deterministic_load_bearing":   <int>,
--     "critic_finding_count":         <int>,
--     "load_bearing_critic_findings": [{file, line, summary}, ...],
--     "scorer_rubric":                {factual, consistency, ...},
--     "short_circuited":              <bool>
--   }

PRAGMA foreign_keys = OFF;

-- 1. Create the rebuilt table with the widened CHECK.
CREATE TABLE IF NOT EXISTS task_events_v2 (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    subtask_id      TEXT NOT NULL,
    from_role       TEXT,
    event_type      TEXT NOT NULL
                    CHECK (event_type IN (
                        'decision', 'supersedes', 'contract',
                        'gap', 'open_question', 'compression',
                        'verdict'
                    )),
    body            TEXT NOT NULL DEFAULT '{}',
    supersedes      TEXT REFERENCES task_events_v2(id),
    confidence      REAL DEFAULT 0.8,
    seq             INTEGER NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    created_by      TEXT,
    UNIQUE (task_id, seq)
);

-- 2. Copy rows from the old table (idempotent — IGNORE on collision so
--    a partially-applied migration can be re-run without duplicates).
INSERT OR IGNORE INTO task_events_v2 (
    id, task_id, subtask_id, from_role, event_type,
    body, supersedes, confidence, seq, created_at, created_by
)
SELECT
    id, task_id, subtask_id, from_role, event_type,
    body, supersedes, confidence, seq, created_at, created_by
FROM task_events;

-- 3. Swap.
DROP TABLE IF EXISTS task_events;
ALTER TABLE task_events_v2 RENAME TO task_events;

-- 4. Recreate indexes + triggers (table swap dropped them).
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
