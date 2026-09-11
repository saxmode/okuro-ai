-- <!-- AGENT_HEADER
-- role: code
-- purpose: 027_artifacts_drop_deliverable_kind — artifacts stores only `report`
--   and `evidence`. Code (shell scripts, migrations, templates) belongs in the
--   git repo. `deliverable` was a category error in 026's original design —
--   conflated version-controlled code with synthesized narrative. Revert that.
-- index: content
-- AGENT_HEADER_END -->

-- Systems-over-symptom rationale: every content type has ONE natural home.
--   code / scripts / templates            → git
--   secrets                                → keyring
--   user profile                           → user_profile
--   facts / gotchas / decisions            → agent_memory
--   session activity                       → sessions / trace
--   synthesized narratives (reports)       → artifacts.report
--   captured raw output (logs, probes)     → artifacts.evidence
--   user ideas / action items              → thoughts
-- `deliverable` created overlap with git. Git always wins for code (version
-- history, diff, review, executable semantics, permissions). Remove the
-- overlap at the schema layer so future agents can't recreate it.
--
-- Enforcement: replace the CHECK constraint on kind to exclude 'deliverable'.
-- SQLite 3.25+ allows dropping constraints via table-rebuild; we use the
-- documented 12-step procedure (https://sqlite.org/lang_altertable.html#otheralter).
-- Any existing 'deliverable' rows are refused by the rebuild — callers must
-- have ported that content to git before running this migration. The
-- preceding commit in this PR already did that for mac-wipe.sh.

-- Natural enforcement: any remaining `deliverable` rows fail the new
-- CHECK constraint during INSERT-SELECT below, rolling back the whole
-- migration. Operators hitting that abort must: (1) extract the body to
-- the appropriate git repo, (2) DELETE the deliverable rows, (3) re-run.
-- (A pre-flight SELECT RAISE would be clearer but SQLite only permits
-- RAISE inside trigger programs.)

-- Rebuild artifacts with the tighter CHECK. SQLite cannot ALTER CHECK in
-- place; the documented procedure is new-table + INSERT-SELECT + DROP + RENAME.
-- Triggers and indexes are dropped with the old table; we recreate them
-- identical to 026 minus the deliverable kind.

PRAGMA foreign_keys = OFF;

DROP TRIGGER IF EXISTS artifacts_reject_empty_id_ins;
DROP TRIGGER IF EXISTS artifacts_reject_empty_title_ins;
DROP TRIGGER IF EXISTS artifacts_touch_updated_at;

CREATE TABLE artifacts_new (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('report','evidence')),
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
