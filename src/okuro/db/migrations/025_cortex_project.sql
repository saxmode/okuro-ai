-- <!-- AGENT_HEADER
-- role: code
-- purpose: 025_cortex_project — project column on cortex_docs for multi-root indexing.
-- index: content
-- AGENT_HEADER_END -->

-- Multi-project cortex index. Before this migration every row in cortex_docs
-- was implicitly tied to OKURO_ROOT (the workspace repo) because only one root was
-- ever indexed. The project column lets the same DB hold documents from
-- multiple repos (workspace, okuro, future projects) and lets cortex_search
-- filter by project slug instead of a path-prefix substring.
--
-- NULL is allowed for rows indexed before this column existed; readers that
-- want strict filtering should treat NULL as "unknown project".
--
-- cortex_docs is created lazily by VectorStore._ensure_schema(), so we
-- CREATE TABLE IF NOT EXISTS first (mirroring the 009 pattern) and only
-- ALTER if the column is missing.

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
    indexed_at  TEXT DEFAULT (datetime('now')),
    deleted_at  TEXT DEFAULT NULL
);

-- SQLite can't do "ADD COLUMN IF NOT EXISTS", so we swallow the duplicate
-- error via a no-op trick: try-then-ignore at runtime. Here in SQL, rely on
-- the migration runner's transactional rollback — running this migration
-- twice is a bug, so we just add it once.
ALTER TABLE cortex_docs ADD COLUMN project TEXT;

CREATE INDEX IF NOT EXISTS idx_cortex_docs_project
    ON cortex_docs(project, deleted_at);
