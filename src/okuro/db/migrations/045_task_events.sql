-- <!-- AGENT_HEADER
-- role: code
-- purpose: 045_task_events — append-only typed event log per task (M2).
--   Orthogonal to role_handovers (035) — that table stays as Stream A
--   per-subtask handover. task_events is the cross-subtask decision log
--   the compressor agent reads to produce decision-trace context for the
--   next serial step. Disk mirror lives at
--   ~/.okuro/orchestrator/tasks/{task_id}/events.jsonl for replay + grep.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a new table, not extending role_handovers:
--   role_handovers is one-row-per-subtask-attempt with brief=JSON blob.
--   task_events is many-rows-per-subtask with one typed event per row,
--   ordered by seq, conflict-detected at append. The two shapes do not
--   compose without forcing one into the other's semantics. M2 keeps the
--   mature handover path untouched; conflict-detect-at-append + ADR
--   projection live here.
--
-- Why no FK on task_id / subtask_id:
--   Orchestrator state is JSON-on-disk under ~/.okuro/orchestrator/tasks/.
--   No SQL `tasks` table. Same rationale as 035.
--
-- Event types (CHECK-constrained):
--   decision        — a load-bearing choice (stack pick, lib pick, etc.)
--   supersedes      — explicit retraction of a prior event (links via supersedes col)
--   contract        — typed contract surface (api/schema/type/envelope/event/config)
--   gap             — a known-unknown the producer flagged for downstream
--   open_question   — same as gap but explicitly requires user-side answer
--   compression     — output of the compressor agent (decision-trace artifact)

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS task_events (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    subtask_id      TEXT NOT NULL,
    from_role       TEXT,
    event_type      TEXT NOT NULL
                    CHECK (event_type IN (
                        'decision', 'supersedes', 'contract',
                        'gap', 'open_question', 'compression'
                    )),
    body            TEXT NOT NULL DEFAULT '{}',
    supersedes      TEXT REFERENCES task_events(id),
    confidence      REAL DEFAULT 0.8,
    seq             INTEGER NOT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    created_by      TEXT,
    UNIQUE (task_id, seq)
);

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
