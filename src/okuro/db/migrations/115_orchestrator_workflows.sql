-- <!-- AGENT_HEADER
-- role: code
-- purpose: 115_orchestrator_workflows — persistence for MANUALLY ARRANGED
--   orchestrator workflows: node graphs a human (or an agent) draws, with a
--   role + prompt + acceptance criteria per node, compiled to a plan by
--   okuro.orchestrator.flow_compiler. Same DDL shape as flow_designer (064/065/
--   070) because both bind the SAME mechanism, okuro.graphdoc.GraphDocStore.
-- index: content
-- AGENT_HEADER_END -->
--
-- WHY SEPARATE TABLES rather than a `kind` column on flow_designer:
-- okuro-flow (/flow) is a standalone VISUALIZER of complex information and must
-- not be corrupted by orchestration semantics. Separate tables make that
-- structural — a workflow cannot appear in the /flow gallery because it is not
-- in that table at all, rather than because every query remembered to filter.
-- The two share code (GraphDocStore), never rows.
--
-- Node semantics live in the graph JSON (node.data: kind='subtask', phase, role,
-- prompt, acceptance_criteria, risk, complexity, outputs, serialize, fan_out) and
-- are interpreted ONLY by flow_compiler. The storage layer stays dumb, exactly
-- as it is for okuro-flow.
--
-- Column names deliberately mirror flow_designer, including `flow_id` in the
-- events/history tables: GraphDocStore issues the same SQL against either table
-- set, so renaming them here would mean a second code path.
--
-- RENUMBERED 113 -> 115 on 2026-07-26. This file shipped as 113 minutes before
-- another session's 113_interaction_analysis.sql landed on the same number, and
-- _check_gap RAISES MigrationGapError on a duplicate — which broke migrate() for
-- every FRESH database (new installs, CI, the test sandbox), while already-
-- migrated databases carried on unaffected. Renumbering is safe because every
-- statement here is CREATE ... IF NOT EXISTS: a database that already ran it as
-- 113 simply re-runs it as 115 and changes nothing.

CREATE TABLE IF NOT EXISTS orchestrator_workflows (
    id TEXT PRIMARY KEY,                                  -- slug, unique
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    graph TEXT NOT NULL DEFAULT '{"nodes":[],"edges":[]}',-- JSON {nodes,edges,viewport?,settings?}
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    rev INTEGER NOT NULL DEFAULT 0,
    folder_id TEXT,                                       -- NULL = ungrouped
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_workflows_updated
    ON orchestrator_workflows(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_orchestrator_workflows_folder
    ON orchestrator_workflows(folder_id);

CREATE TABLE IF NOT EXISTS orchestrator_workflows_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL,                                -- workflow id (shared SQL, see above)
    kind TEXT NOT NULL CHECK (kind IN ('saved', 'deleted')),
    origin TEXT NOT NULL DEFAULT '',                      -- writer client id (echo suppression)
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_workflows_events_seq
    ON orchestrator_workflows_events(seq);

CREATE TABLE IF NOT EXISTS orchestrator_workflows_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL,
    graph TEXT NOT NULL,                                  -- snapshot of the PRIOR graph blob
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    rev INTEGER NOT NULL DEFAULT 0,                       -- rev of the snapshotted (prior) version
    origin TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_workflows_history_flow
    ON orchestrator_workflows_history(flow_id, seq DESC);

CREATE TABLE IF NOT EXISTS orchestrator_workflow_folders (
    id TEXT PRIMARY KEY,                                  -- slug, unique
    name TEXT NOT NULL,
    parent_id TEXT,                                       -- NULL = top level
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_workflow_folders_parent
    ON orchestrator_workflow_folders(parent_id);

-- ROLLBACK
-- okuro migrations are forward-only (numbered .sql, no down-migration runner),
-- so this is documented rather than executable. It is safe and complete because
-- the migration is purely ADDITIVE: four new tables, no ALTER of any existing
-- table, no data backfill. Dropping them cannot affect okuro-flow or anything
-- else — nothing outside this feature reads them.
--
--   DROP TABLE IF EXISTS orchestrator_workflows_history;
--   DROP TABLE IF EXISTS orchestrator_workflows_events;
--   DROP TABLE IF EXISTS orchestrator_workflow_folders;
--   DROP TABLE IF EXISTS orchestrator_workflows;
--   DELETE FROM _migrations WHERE name = '115_orchestrator_workflows.sql';
--   -- and, on a database migrated before 2026-07-26, also:
--   DELETE FROM _migrations WHERE name = '113_orchestrator_workflows.sql';
--
-- Cost of rolling back: every drawn workflow and its history is lost. Export
-- anything worth keeping (SELECT id, name, graph FROM orchestrator_workflows)
-- before running it.
