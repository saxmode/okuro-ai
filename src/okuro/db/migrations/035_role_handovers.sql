-- <!-- AGENT_HEADER
-- role: code
-- purpose: 035_role_handovers — agent-to-agent subtask handover storage.
--   Replaces the dispatcher's "first 40 lines of dep .md" pattern with a
--   structured row + KG mirror + cortex refs. Stream A only — Stream B
--   (user-facing report) lands in `artifacts` (kind='report') and is keyed
--   by the new task_id/subtask_id columns added in this same migration.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a new table, not piggyback on `artifacts`:
--   The artifacts CHECK constraint (027) deliberately excludes new kinds —
--   adding a kind requires distinct lifecycle, distinct consumer, AND
--   distinct supersession semantics. Role-handovers are agent-to-agent
--   structured payloads with their own validators (V1-V8), KG fan-out, and
--   consumed_at lifecycle — own table.
--
-- Why no FK on task_id / subtask_id:
--   Orchestrator state lives as JSON-on-disk under
--   ~/.okuro/orchestrator/tasks/{id}/. There is no SQL `tasks` table.
--   FKs would lie. Treat as opaque TEXT.
--
-- Stream B routing — artifacts gain task_id/subtask_id:
--   Subagent .md deliverables move from disk to artifact_write(kind='report',
--   task_id=…, subtask_id=…). ArtifactsViewer reads via artifact_list
--   filtered on task_id. Engine ghost-completion guard switches from
--   disk-verify to DB-verify (artifact row presence). state.save_artifact
--   stays only for non-document outputs (binaries, screenshots).

PRAGMA foreign_keys = OFF;

-- ---------------------------------------------------------------------------
-- 1. role_handovers (Stream A)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS role_handovers (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    subtask_id      TEXT NOT NULL,
    -- from_role / to_role are opaque TEXT — no FK to roles(role_id).
    -- subtask.role accepts any string the user puts in plan.yaml; an FK
    -- here would reject every legacy or ad-hoc role at write time. Same
    -- rationale as task_id / subtask_id: enforcement belongs at the
    -- orchestrator boundary, not in the storage layer.
    from_role       TEXT,
    to_subtask_id   TEXT,
    to_role         TEXT,
    brief           TEXT NOT NULL DEFAULT '{}',
    cortex_refs     TEXT NOT NULL DEFAULT '[]',
    kg_edges        TEXT NOT NULL DEFAULT '[]',
    artifact_refs   TEXT NOT NULL DEFAULT '[]',
    memory_refs     TEXT NOT NULL DEFAULT '[]',
    supersedes      TEXT REFERENCES role_handovers(id),
    status          TEXT NOT NULL DEFAULT 'success'
                    CHECK (status IN ('success', 'partial', 'failed')),
    confidence      REAL DEFAULT 0.8,
    created_by      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    consumed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_role_handovers_task       ON role_handovers(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_role_handovers_subtask    ON role_handovers(subtask_id);
CREATE INDEX IF NOT EXISTS idx_role_handovers_from       ON role_handovers(from_role);
CREATE INDEX IF NOT EXISTS idx_role_handovers_to         ON role_handovers(to_subtask_id, to_role);
CREATE INDEX IF NOT EXISTS idx_role_handovers_status     ON role_handovers(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_role_handovers_supersedes ON role_handovers(supersedes);

-- Garbage rejection (mirrors 026/008 patterns)
CREATE TRIGGER IF NOT EXISTS role_handovers_reject_empty_id_ins
BEFORE INSERT ON role_handovers
FOR EACH ROW
WHEN NEW.id IS NULL OR TRIM(NEW.id) = ''
BEGIN
    SELECT RAISE(ABORT, 'role_handovers.id must be a non-empty string');
END;

CREATE TRIGGER IF NOT EXISTS role_handovers_reject_empty_subtask_ins
BEFORE INSERT ON role_handovers
FOR EACH ROW
WHEN NEW.subtask_id IS NULL OR TRIM(NEW.subtask_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'role_handovers.subtask_id must be a non-empty string');
END;

-- ---------------------------------------------------------------------------
-- 2. vec_role_handovers — semantic search ("how did the prior planner end?")
--    Parallel to vec_artifacts/vec_memory. Best-effort; sqlite-vec optional.
-- ---------------------------------------------------------------------------

-- vec_role_handovers created post-migrate by ensure_vec_dims() at active tier dim.

-- ---------------------------------------------------------------------------
-- 3. kg_triples.source_handover_id — let KG cite a handover for lineage,
--    parallel to source_memory_id / source_artifact_id (031_kg.sql).
-- ---------------------------------------------------------------------------

ALTER TABLE kg_triples ADD COLUMN source_handover_id TEXT REFERENCES role_handovers(id);
CREATE INDEX IF NOT EXISTS idx_kg_triples_handover ON kg_triples(source_handover_id);

-- ---------------------------------------------------------------------------
-- 4. artifacts.task_id / artifacts.subtask_id — Stream B routing.
--    Document-shaped subagent deliverables move from disk to artifact_write.
--    No FK (orchestrator state JSON-on-disk).
-- ---------------------------------------------------------------------------

ALTER TABLE artifacts ADD COLUMN task_id    TEXT;
ALTER TABLE artifacts ADD COLUMN subtask_id TEXT;
CREATE INDEX IF NOT EXISTS idx_artifacts_task_id    ON artifacts(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_subtask_id ON artifacts(task_id, subtask_id);

PRAGMA foreign_keys = ON;
