-- <!-- AGENT_HEADER
-- role: code
-- purpose: 009_cortex_soft_delete — tombstone column for cortex_docs.
-- index: content
-- AGENT_HEADER_END -->

-- Soft-delete for cortex index rows. When a file is removed from disk the
-- daemon's `cortex_prune_stale` handler sets `deleted_at = datetime('now')`
-- instead of DELETE, so:
--
--   * history is preserved — you can SELECT what the index used to know
--   * accidental deletions are reversible (set deleted_at = NULL)
--   * re-indexing a re-appeared file naturally resurrects the row (the
--     re-index path does DELETE-then-INSERT, so the new row comes back
--     with deleted_at = NULL by default)
--
-- Search/read functions filter `WHERE deleted_at IS NULL` so tombstoned
-- rows no longer surface.
--
-- Layout note: `cortex_docs` is normally created lazily by
-- `okuro.cortex.vectorstore.VectorStore` on first import. On fresh DBs
-- this migration might run before that module is loaded, so we
-- `CREATE TABLE IF NOT EXISTS` first (mirroring the live schema WITHOUT
-- `deleted_at`), then ALTER to add the new column. Both paths — fresh
-- and existing — converge at the same final schema.

CREATE TABLE IF NOT EXISTS cortex_docs (
    id          TEXT PRIMARY KEY,
    file_path   TEXT NOT NULL,
    file_hash   TEXT,
    doc_type    TEXT,
    role        TEXT,
    purpose     TEXT,
    section     TEXT,
    chunk_index INTEGER,
    document    TEXT,
    indexed_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cortex_docs_file
    ON cortex_docs(file_path);
CREATE INDEX IF NOT EXISTS idx_cortex_docs_type
    ON cortex_docs(doc_type);

ALTER TABLE cortex_docs ADD COLUMN deleted_at TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_cortex_docs_active
    ON cortex_docs(file_path, deleted_at);
