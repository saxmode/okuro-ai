-- Migration 111 — reviews: contextual, append-only feedback on ANY okuro surface.
--
-- The ask: "the review could be used from everywhere in okuro. All the pages,
-- all the functions should be having a review functionality. this gives me as a
-- user the possibility to write everything down in context that i don't like."
-- Four requirements: universal, context-capturing, auto-assembles into todos,
-- time-dependent (re-reviewable, timestamped).
--
-- WHY NOT EXTEND flow_feedback (108). That table is task_id PRIMARY KEY and a
-- re-rate REPLACEs the row. That cardinality is correct for what it answers —
-- a flow has exactly one terminal outcome — and TWO live consumers depend on
-- exactly-one-current-row: the learning join (feedback.decisions_for_task) and
-- the forward-note queue (feedback.unresolved_forward_comments). Making it
-- append-only would re-inject every historical note into every future plan
-- prompt. So `reviews` is a second table answering a different question, and
-- 108 is left untouched. The task-detail card dual-writes: upsert
-- flow_feedback (learning loop keeps working) + append one reviews row
-- (history). See the review-capability design artifact for the full argument.
--
-- IDENTITY vs CONTEXT — the load-bearing distinction.
--   surface_id  = IDENTITY. A DECLARED name ("task.flow-feedback-card"), not
--                 something derived from where the user happened to be. Every
--                 derivable candidate rots: this app has already renamed
--                 /tasks -> /work, /roles -> /agents, /dashboard -> /agents,
--                 /system -> /health, and a component path or DOM selector dies
--                 on the next refactor.
--   route etc.  = CONTEXT/evidence. Recorded so a review can be replayed and
--                 understood, never used to identify what was reviewed.
--   entity ref  = WHAT IT WAS ABOUT. (target_type, target_id) follows the
--                 polymorphic convention already established by note_links
--                 (071_notes.sql:42-50), including its 'unresolved' escape
--                 hatch, rather than inventing a second shape.
--
-- TIME-DEPENDENCE. Append-only: one row per submission, created_at never
-- overwritten. "What does he think NOW" = latest row per surface_id. "Is it
-- getting better" = the series. Nothing is ever updated in place except
-- todo_id (stamped once, when assembly runs).
--
-- TODO ASSEMBLY is gated on severity at WRITE time, deterministically — not by
-- an async classifier. The todo store previously accreted to 1236 open / 26
-- done with visible duplicates (todos.py:169-172); "every review becomes a
-- todo" would recreate that at higher volume. blocker/annoyance create one,
-- idea/praise do not. The writer MUST pass source_event_id = review id, or
-- todo_add's title-based dedup silently merges two reviews of different
-- surfaces into one todo (todos.py:87-117).
--
-- Rollback (SQLite forward-only):
--   DROP TABLE IF EXISTS reviews;
--   DROP TABLE IF EXISTS review_surfaces;

CREATE TABLE IF NOT EXISTS reviews (
    id            TEXT PRIMARY KEY,              -- uuid4; also the todo source_event_id
    -- IDENTITY. Declared by the surface itself. A route-level default is
    -- registered automatically ("route:/work/:id"), so every page is
    -- reviewable with zero per-component work; components declare a finer id
    -- only where they want the granularity.
    surface_id    TEXT NOT NULL,

    -- The review itself. rating is OPTIONAL: "I don't like the visibility of
    -- component X" is a complete review with no score, and forcing one would
    -- make the fast path lie.
    rating        INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    comment       TEXT,
    -- Closed vocabulary, and the todo gate. Deliberately NOT open-ended: it
    -- drives a create/skip decision, so a typo must fail loudly at write time.
    severity      TEXT NOT NULL DEFAULT 'annoyance' CHECK (
        severity IN ('blocker', 'annoyance', 'idea', 'praise')
    ),

    -- WHAT IT WAS ABOUT. Same polymorphic shape as note_links (071:42-50).
    target_type   TEXT NOT NULL DEFAULT 'unresolved' CHECK (
        target_type IN ('task', 'subtask', 'note', 'person', 'project',
                        'thought', 'artifact', 'role', 'deck', 'kg',
                        'unresolved')
    ),
    target_id     TEXT,                          -- NULL when unresolved

    -- CONTEXT / evidence. Never identity.
    route         TEXT,                          -- pattern, e.g. /work/:id
    route_params  TEXT,                          -- JSON of the resolved params
    viewport      TEXT,                          -- "1440x900" — layout complaints need it
    app_version   TEXT,                          -- what was on screen when said

    -- Option D (review-mode overlay). Columns exist now so enabling the
    -- overlay is a frontend change with NO migration: a click on an
    -- undeclared element resolves to its nearest declared ancestor and records
    -- the DOM path as a HINT — never as identity, because it dies on markup
    -- change.
    dom_hint        TEXT,
    screenshot_ref  TEXT,                        -- artifact id, when captured

    -- Assembly result. Stamped once; NULL means no todo was created (by the
    -- severity gate, by the user's override, or because assembly has not run).
    todo_id       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- "What does he think of this surface now / over time" — the two main reads.
CREATE INDEX IF NOT EXISTS idx_reviews_surface ON reviews(surface_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_target  ON reviews(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_reviews_todo    ON reviews(todo_id);


-- Registry of surfaces that have ever declared themselves. The frontend POSTs
-- its live catalogue on boot; a row absent from that catalogue is stamped
-- orphaned_at.
--
-- TOMBSTONE, NEVER CASCADE-DELETE. A removed surface keeps its reviews
-- readable and its todos open — the complaint may be the very reason it was
-- removed. Orphans simply drop out of "current surface health" rollups.
CREATE TABLE IF NOT EXISTS review_surfaces (
    surface_id    TEXT PRIMARY KEY,
    label         TEXT,                          -- human name for the UI
    route         TEXT,                          -- where it was last seen
    -- Set when the surface stops appearing in the live catalogue. NULL = live.
    orphaned_at   TEXT,
    -- A rename carries history forward instead of orphaning it.
    superseded_by TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_review_surfaces_live
    ON review_surfaces(orphaned_at);
