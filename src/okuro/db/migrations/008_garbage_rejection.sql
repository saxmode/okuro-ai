-- <!-- AGENT_HEADER
-- role: code
-- purpose: 008_garbage_rejection — insertion-time gates for progress + agent_memory.
-- index: content
-- AGENT_HEADER_END -->

-- A7 — DB garbage can't enter.
--
-- Observed in the 2026-04-16 audit:
--   * 8 of 8 rows in `progress` had empty PRIMARY KEY (SQLite allows empty
--     strings for TEXT primary keys — it satisfies UNIQUE but is useless).
--   * 128 of 286 rows in `agent_memory` were bulk-imported doc dumps with
--     little value, some with near-empty content.
--
-- Fix: BEFORE-INSERT/UPDATE triggers that `RAISE(ABORT)` on garbage values.
-- Chose triggers over table rebuild because:
--   * agent_memory has FK children (vec_memory, supersedes self-reference);
--     rebuilding would require transient breakage.
--   * Triggers activate without data migration; existing rows are untouched
--     (the daemon's memory_hygiene handler cleans them on schedule).

-- --------------------------------------------------------------------
-- progress: empty id or empty summary rejected
-- --------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS progress_reject_empty_id_insert
BEFORE INSERT ON progress
FOR EACH ROW
WHEN NEW.id IS NULL OR length(NEW.id) = 0
BEGIN
    SELECT RAISE(ABORT, 'progress.id must be a non-empty string');
END;

CREATE TRIGGER IF NOT EXISTS progress_reject_empty_id_update
BEFORE UPDATE OF id ON progress
FOR EACH ROW
WHEN NEW.id IS NULL OR length(NEW.id) = 0
BEGIN
    SELECT RAISE(ABORT, 'progress.id must be a non-empty string');
END;

CREATE TRIGGER IF NOT EXISTS progress_reject_short_summary_insert
BEFORE INSERT ON progress
FOR EACH ROW
WHEN NEW.summary IS NULL OR length(NEW.summary) < 5
BEGIN
    SELECT RAISE(ABORT, 'progress.summary must be at least 5 characters');
END;

-- UPDATE OF <col> only fires when that column is actually assigned in
-- the UPDATE statement. This lets `UPDATE progress SET updated_at=...`
-- (unrelated columns) pass even if the existing summary is historically
-- short, while still blocking any attempt to SET a short summary.
CREATE TRIGGER IF NOT EXISTS progress_reject_short_summary_update
BEFORE UPDATE OF summary ON progress
FOR EACH ROW
WHEN NEW.summary IS NULL OR length(NEW.summary) < 5
BEGIN
    SELECT RAISE(ABORT, 'progress.summary must be at least 5 characters');
END;

-- --------------------------------------------------------------------
-- agent_memory: content must be substantive
-- --------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS agent_memory_reject_short_content_insert
BEFORE INSERT ON agent_memory
FOR EACH ROW
WHEN NEW.content IS NULL OR length(trim(NEW.content)) < 20
BEGIN
    SELECT RAISE(ABORT, 'agent_memory.content must be >= 20 chars (no trivial/placeholder memories)');
END;

-- UPDATE OF content — same reasoning as progress.summary. Decay, access
-- bumps, and supersedes-linking touch other columns and must pass.
CREATE TRIGGER IF NOT EXISTS agent_memory_reject_short_content_update
BEFORE UPDATE OF content ON agent_memory
FOR EACH ROW
WHEN NEW.content IS NULL OR length(trim(NEW.content)) < 20
BEGIN
    SELECT RAISE(ABORT, 'agent_memory.content must be >= 20 chars');
END;

-- --------------------------------------------------------------------
-- One-time cleanup: quarantine existing garbage rows into *_quarantine
-- tables so users can recover them if needed, then delete from the live
-- tables. Re-running the migration is a no-op (quarantine tables exist
-- with IF NOT EXISTS; the DELETE affects only rows that still match).
-- --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS progress_quarantine (
    id              TEXT,
    project         TEXT,
    agent           TEXT,
    status          TEXT,
    summary         TEXT,
    files_touched   TEXT,
    next_steps      TEXT,
    blockers        TEXT,
    branch          TEXT,
    history         TEXT,
    started_at      TEXT,
    updated_at      TEXT,
    quarantined_at  TEXT DEFAULT (datetime('now')),
    quarantine_reason TEXT
);

INSERT INTO progress_quarantine
    (id, project, agent, status, summary, files_touched, next_steps,
     blockers, branch, history, started_at, updated_at, quarantine_reason)
SELECT
    id, project, agent, status, summary, files_touched, next_steps,
    blockers, branch, history, started_at, updated_at,
    CASE
        WHEN id IS NULL OR length(id) = 0 THEN 'empty_id'
        WHEN summary IS NULL OR length(summary) < 5 THEN 'short_summary'
        ELSE 'other'
    END
FROM progress
WHERE id IS NULL OR length(id) = 0
   OR summary IS NULL OR length(summary) < 5;

DELETE FROM progress
WHERE id IS NULL OR length(id) = 0
   OR summary IS NULL OR length(summary) < 5;
