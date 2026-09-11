-- <!-- AGENT_HEADER
-- role: code
-- purpose: 034_artifacts_add_plan_kind — add `plan` to the artifacts CHECK
--   constraint. Plans are pre-execution architectural/execution documents
--   (migration plans, design specs, ADRs) — distinct lifecycle and consumer
--   from `report` (post-execution synthesis) and `evidence` (raw captures).
-- index: content
-- AGENT_HEADER_END -->

-- Why a new kind, not a synonym:
--   plan      = pre-execution; superseded as scope shifts; closed when executed
--   report    = post-execution synthesis; usually terminal
--   evidence  = raw captured output a report cites
-- Adding kinds requires distinct lifecycle, distinct consumer, AND distinct
-- supersession semantics — not just "different content". `plan` clears that
-- bar; future kinds must demonstrate the same.
--
-- SQLite cannot ALTER CHECK in place (same constraint as 027). Use the
-- documented table-rebuild procedure: new table with widened CHECK,
-- INSERT-SELECT, DROP, RENAME. Triggers and indexes recreated identically.

PRAGMA foreign_keys = OFF;

DROP TRIGGER IF EXISTS artifacts_reject_empty_id_ins;
DROP TRIGGER IF EXISTS artifacts_reject_empty_title_ins;
DROP TRIGGER IF EXISTS artifacts_touch_updated_at;

CREATE TABLE artifacts_new (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('report','evidence','plan')),
    title        TEXT NOT NULL,
    summary      TEXT,
    body         TEXT,
    body_blob    BLOB,
    media_type   TEXT,
    project      TEXT REFERENCES projects(id),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now')),
    created_by   TEXT,
    parent_id    TEXT REFERENCES artifacts_new(id),
    supersedes   TEXT REFERENCES artifacts_new(id),
    memory_refs  TEXT DEFAULT '[]',
    confidence   REAL DEFAULT 0.8
);

INSERT INTO artifacts_new
SELECT id, kind, title, summary, body, body_blob, media_type, project,
       created_at, updated_at, created_by, parent_id, supersedes,
       memory_refs, confidence
FROM artifacts;

DROP TABLE artifacts;
ALTER TABLE artifacts_new RENAME TO artifacts;

CREATE INDEX IF NOT EXISTS idx_artifacts_kind       ON artifacts(kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_project    ON artifacts(project);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent     ON artifacts(parent_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_supersedes ON artifacts(supersedes);
CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at DESC);

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

CREATE TRIGGER IF NOT EXISTS artifacts_touch_updated_at
AFTER UPDATE ON artifacts
FOR EACH ROW
BEGIN
    UPDATE artifacts SET updated_at = datetime('now') WHERE id = OLD.id;
END;

PRAGMA foreign_keys = ON;
