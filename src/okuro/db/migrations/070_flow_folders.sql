-- <!-- AGENT_HEADER
-- role: code
-- purpose: 070_flow_folders — nestable folders for okuro-flow diagrams. A flow
--   may belong to one folder; folders nest via parent_id. Powers the /flow
--   gallery's left-rail tree. Soft references (no hard FK) — deletes reconcile
--   in storage (children reparent to the removed folder's parent).
-- index: content
-- AGENT_HEADER_END -->
--
-- flow_folders is a plain adjacency-list tree. `parent_id` NULL = top level.
-- flow_designer.folder_id NULL = ungrouped. Both are soft references kept
-- consistent by okuro.flow_designer.storage (SQLite ALTER can't add FKs after
-- the fact, and a single-user tool doesn't need the enforcement overhead).

CREATE TABLE IF NOT EXISTS flow_folders (
    id TEXT PRIMARY KEY,                                  -- slug, unique
    name TEXT NOT NULL,
    parent_id TEXT,                                       -- NULL = top level
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_flow_folders_parent ON flow_folders(parent_id);

-- a flow's folder (NULL = ungrouped). Added to the existing table.
ALTER TABLE flow_designer ADD COLUMN folder_id TEXT;

CREATE INDEX IF NOT EXISTS idx_flow_designer_folder ON flow_designer(folder_id);
