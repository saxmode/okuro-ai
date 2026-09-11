-- <!-- AGENT_HEADER
-- role: code
-- purpose: 026_artifacts — first-class storage for reports/evidence/deliverables.
-- index: content
-- AGENT_HEADER_END -->

-- Artifacts table — parallel to agent_memory, thoughts, progress, persons.
--
-- Why this exists: agent_memory is a POINTER (atomic facts, <800 chars).
-- Long compositions (audit reports, pentest protocols, evidence dumps, shell
-- deliverables) do not fit the memory shape and get rejected by the
-- _looks_like_document guardrail. Before this table the only alternative was
-- writing .md files on disk — which violates the no-docs convention and
-- rots out of sync with the code. Artifacts is the correct shape for
-- "long, attributable, searchable composition".
--
-- Vec table uses the vec_<table> prefix convention matching vec_memory,
-- vec_thoughts, vec_persons. The embedding input is
-- title + summary + first ~500 chars of body — NOT full body.

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('report','evidence','deliverable')),
    title        TEXT NOT NULL,
    summary      TEXT,
    body         TEXT,
    body_blob    BLOB,
    media_type   TEXT,
    project      TEXT REFERENCES projects(id),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now')),
    created_by   TEXT,
    parent_id    TEXT REFERENCES artifacts(id),
    supersedes   TEXT REFERENCES artifacts(id),
    memory_refs  TEXT DEFAULT '[]',
    confidence   REAL DEFAULT 0.8
);

CREATE INDEX IF NOT EXISTS idx_artifacts_kind       ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_project    ON artifacts(project);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent     ON artifacts(parent_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_supersedes ON artifacts(supersedes);
CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at DESC);

-- Mirror the garbage-rejection pattern from migration 008 for the two
-- hard-required columns, so no caller (python, raw SQL, future MCP) can
-- write empty PKs or empty titles.
CREATE TRIGGER IF NOT EXISTS artifacts_reject_empty_id_ins
BEFORE INSERT ON artifacts
FOR EACH ROW
WHEN NEW.id IS NULL OR TRIM(NEW.id) = ''
BEGIN
    SELECT RAISE(ABORT, 'artifacts.id must be a non-empty string');
END;

CREATE TRIGGER IF NOT EXISTS artifacts_reject_empty_title_ins
BEFORE INSERT ON artifacts
FOR EACH ROW
WHEN NEW.title IS NULL OR LENGTH(TRIM(NEW.title)) < 3
BEGIN
    SELECT RAISE(ABORT, 'artifacts.title must be at least 3 characters');
END;

-- Maintain updated_at on every row mutation.
CREATE TRIGGER IF NOT EXISTS artifacts_touch_updated_at
AFTER UPDATE ON artifacts
FOR EACH ROW
BEGIN
    UPDATE artifacts SET updated_at = datetime('now') WHERE id = OLD.id;
END;

-- vec_artifacts created post-migrate by ensure_vec_dims() at active tier dim.
