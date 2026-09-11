-- Migration 108 — flow_feedback: the user's post-flow rating + forward note.
--
-- F3 closes the learning loop opened by F1 (continue-on-exhausted-review) and
-- F2 (in-process autopilot). F2 made every gate answer itself and emit a
-- durable `autopilot_decision` event into each task's log.jsonl. Until now
-- those auto-answers carried NO outcome signal — the engine could not tell a
-- good auto-answer from a bad one, so nothing could learn. F3 attaches the
-- missing label: after a flow finishes the user rates it, and that rating
-- (a) back-labels every autopilot_decision the flow emitted (the join lives in
-- okuro.orchestrator.feedback.decisions_for_task — NOT in this schema; the
-- events stay in log.jsonl, this table is keyed by task_id to join to them),
-- and (b) carries a free-text forward note injected into the NEXT flow's plan
-- prompt (decomposer.decompose_task) so prior-flow guidance shapes future work.
--
-- ONE ROW PER TASK. task_id is the PRIMARY KEY — a re-rating REPLACEs the row
-- (the API uses INSERT ... ON CONFLICT(task_id) DO UPDATE). A flow has exactly
-- one terminal outcome, so one feedback row is the correct cardinality; history
-- of re-rates is not a requirement F3 states, and modelling it would add a
-- second table nothing consumes (DP09).
--
-- outcome_class is a CLOSED enum enforced by CHECK — unlike migration 105's
-- deliberately-open `mechanism`, this is a fixed rating vocabulary driven by a
-- UI <select>, not a set that new code must self-register into. Widening it is
-- a deliberate product decision that warrants a migration, not a silent write.
-- The API validates it too (Pydantic) so the caller gets a 422 with the allowed
-- values rather than an opaque SQLite CHECK failure.
--
-- resolved_at marks a forward comment as CONSUMED. decompose_task injects the
-- most-recent-N unresolved comments then stamps resolved_at, so a note shapes
-- the next flow once and does not echo into every subsequent plan (bounded +
-- non-repeating — the anti-clutter contract). NULL = still pending injection.
--
-- Rollback (SQLite forward-only): DROP TABLE IF EXISTS flow_feedback;

CREATE TABLE IF NOT EXISTS flow_feedback (
    task_id        TEXT PRIMARY KEY,
    -- Usability score, 1 (unusable) .. 5 (excellent). Small int by convention.
    usability      INTEGER NOT NULL CHECK (usability BETWEEN 1 AND 5),
    -- Closed outcome vocabulary. success = did what was asked; partial = some
    -- value, gaps remain; failed = did not deliver; off_track = solved the
    -- wrong problem / drifted from intent; needs_rework = delivered but the
    -- result must be redone.
    outcome_class  TEXT NOT NULL CHECK (
        outcome_class IN ('success', 'partial', 'failed', 'off_track', 'needs_rework')
    ),
    -- The forward note: "for the next flow to consider". NULL when the user
    -- rated without leaving guidance.
    comment        TEXT,
    -- Set when decompose_task injects this comment into a later flow's plan
    -- prompt. NULL = unresolved (still eligible for injection).
    resolved_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The forward-comment feed query: most-recent unresolved comments. Partial
-- index so it stays tiny — only rows that actually carry a pending note.
CREATE INDEX IF NOT EXISTS idx_flow_feedback_unresolved
    ON flow_feedback (created_at DESC)
    WHERE resolved_at IS NULL AND comment IS NOT NULL;
