-- <!-- AGENT_HEADER
-- role: code
-- purpose: 130_project_status module
-- index: content
-- AGENT_HEADER_END -->
-- Project status — a DECLARED phase plan, a DERIVED position within it, and
-- the session key that makes progress joinable to telemetry.
--
-- Three measured holes this closes (all measured 2026-08-07 on the live store):
--
-- 1. "HOW FAR ALONG" HAS NO REPRESENTATION. "P1 done, P2-P7 not started,
--    7 decisions open" existed only inside free-text progress summaries. No
--    query could answer it, so every agent re-derived it by reading prose.
--
-- 2. PROGRESS AND SESSIONS SHARE NO KEY. progress.history entries record
--    status/summary/updated_at/files_touched/next_steps/memory_keys and
--    NOTHING that identifies the session that wrote them (see
--    sense/progress.py::log_progress, the history_entry dict). So
--    session_history(project=X) and get_progress(X, include_history=True)
--    describe overlapping work with no way to correlate them: for
--    okuro-design-systems, progress held 5 entries while sessions held 1 row
--    carrying that slug. The two numbers were never joinable, only comparable.
--
-- 3. sessions.project IS NULL FOR 43% OF SESSIONS (1487 of 3422 over 30 days).
--    It is written once, at bootstrap, from resolve_project(task_hint)
--    (sense/bootstrap/assembler.py -> create_session(project=project_slug)).
--    A session whose task hint never named a project keeps NULL forever, even
--    after it logs progress against that very project.
--
-- WHY A PHASE TABLE AND NOT A COLUMN ON progress.
-- A per-progress-row free-text phase drifts exactly like the prose it would
-- replace: three sessions write "P1", "phase 1" and "scoping" and nothing can
-- count them. The PLAN is a property of the project, declared once and rarely
-- edited; a progress row only REFERENCES a declared key. That split is also
-- what makes the status derivable — see project_phases.state below.

-- The declared plan. One row per phase per project, ordered by seq.
CREATE TABLE IF NOT EXISTS project_phases (
    project     TEXT NOT NULL,
    key         TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    title       TEXT,
    -- pending | active | done | dropped.
    --   pending -> nothing has referenced it yet
    --   active  -> DERIVED: log_progress(phase=key) promotes pending -> active
    --   done    -> DECLARED: only an explicit call sets this. "finished" is a
    --              claim, never an inference; deriving it from activity is how
    --              a status view starts lying.
    --   dropped -> declared out of scope, kept for history
    state       TEXT NOT NULL DEFAULT 'pending',
    note        TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (project, key)
);

CREATE INDEX IF NOT EXISTS idx_project_phases_order
    ON project_phases(project, seq);

-- Additive, nullable, defaulted. ~2700 sessions of existing callers pass
-- neither column and must keep working byte-for-byte; every new column here
-- is NULL for every existing row and optional for every existing caller.
ALTER TABLE progress ADD COLUMN phase TEXT;
ALTER TABLE progress ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_progress_session
    ON progress(session_id);
