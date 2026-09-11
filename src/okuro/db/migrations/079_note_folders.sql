-- <!-- AGENT_HEADER
-- role: code
-- purpose: 079_note_folders — folder tree for okuro-notes. Adjacency-list model
--   (parent_id self-reference) so folders nest arbitrarily, persist while empty,
--   rename/move by touching a single row. notes.folder_id points a note at its
--   containing folder (NULL = vault root). Mirrors the notes (071) DB-storage
--   ethos: structure lives in the DB, no filesystem paths.
-- index: content
-- AGENT_HEADER_END -->
--
-- Adjacency list beats a path string here: empty folders are real rows, a rename
-- is one UPDATE (not a LIKE-prefix bulk rewrite), and a move re-parents one row.
-- The tree is walked in memory from (id, parent_id) — cheap at note-vault scale.
-- ON DELETE is handled in application code (reparent children to the deleted
-- folder's parent, orphan notes back to root) rather than via FK cascade, so the
-- behaviour is explicit and matches the Obsidian-style "never lose a note" rule.

CREATE TABLE IF NOT EXISTS folders (
    id         TEXT PRIMARY KEY,                        -- uuid4
    name       TEXT NOT NULL,
    parent_id  TEXT,                                    -- NULL = root; self-ref
    sort       INTEGER NOT NULL DEFAULT 0,              -- manual ordering within a parent
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

-- A note's containing folder. NULL = root. No FK cascade — delete_folder() in
-- storage.py reparents/orphans explicitly.
ALTER TABLE notes ADD COLUMN folder_id TEXT;

CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_id);
