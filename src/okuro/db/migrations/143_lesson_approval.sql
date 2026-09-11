-- <!-- AGENT_HEADER
-- role: code
-- purpose: 143_lesson_approval — an ACTIVE lesson without a human approval is unrepresentable.
-- index: content
-- AGENT_HEADER_END -->
--
-- Mining is being switched ON. Migration 138 built the lesson pipeline on the
-- assumption that corroboration across a cluster was enough to make a lesson
-- worth acting on; the owner's decision (2026-08-15) is that it is necessary and
-- NOT sufficient. Nothing becomes a rule he has to live under without him
-- having read it first.
--
-- The obvious place to put that is the approve path in Python. That protects
-- exactly one call site, and this table is reachable from a daemon task, a CLI,
-- an MCP tool, a future agent and the sqlite3 binary — the same argument
-- migration 136 makes for the retention guard. So the check moves into the
-- schema, where every writer meets it.
--
-- ---------------------------------------------------------------------------
-- Why the condition is spelled positively
-- ---------------------------------------------------------------------------
-- Same lesson as 137's calibration gate, which was defeated in three
-- statements when it asked "does this LOOK like a judged verdict?". Keying a
-- guard on the shape of the thing you want to catch fails open the moment a
-- writer omits a column — and omitting a column is the easiest mistake in the
-- file.
--
-- So this asks the opposite question. A row is exempt only when it is NOT
-- active. Anything claiming status='active' must positively carry BOTH
-- approval columns, and a writer that omits either is refused rather than
-- waved through:
--
--     approved_by IS NOT NULL AND TRIM(approved_by) != ''   who decided
--     approved_at IS NOT NULL                                when
--
-- Every term is NULL-total. `IS NOT NULL` never yields NULL; `status` is NOT
-- NULL by its own column constraint; and if approved_by is NULL the first
-- conjunct is 0, which makes the whole conjunction 0 in SQLite regardless of
-- what the TRIM comparison would have returned. No input can make the WHEN
-- clause evaluate NULL and let the write through.
--
-- ---------------------------------------------------------------------------
-- What this deliberately does NOT gate: RETIREMENT
-- ---------------------------------------------------------------------------
-- Retirement stays fully automatic and needs no approval, at any threshold, in
-- both directions:
--
--   * The ACE lifecycle retires a lesson whose contrary evidence outweighs its
--     support (lessons.transition).
--   * A human rejects a candidate outright.
--
-- Both write status='retired', which this trigger does not touch. That
-- asymmetry is the point rather than an oversight: REMOVING a bad rule must
-- never wait on a human being available. Gating retirement would mean a lesson
-- measured to be making things worse keeps being proposed until somebody logs
-- in — the failure mode is unbounded and silent, and it is exactly backwards
-- from the risk approval exists to manage. Adding a rule is the irreversible
-- direction; taking one away is the safe one.
--
-- ---------------------------------------------------------------------------
-- What this honestly does not do
-- ---------------------------------------------------------------------------
-- SQL cannot verify that a HUMAN typed the name. `approved_by` is a string and
-- any writer can set it, exactly as 137 says of `approved_by` on a validation
-- batch. What the schema CAN do is make every cheap forgery impossible — a row
-- cannot become active by accident, by a writer that forgot the column, or
-- from a shell that never considered approval at all.
--
-- The complementary half lives in Python: lessons.approve_lesson refuses
-- automation-shaped approvers ('daemon', 'cron', 'auto', …), reusing the same
-- vocabulary rubric.set_min_deletable_rubric_version already applies to the
-- deletion floor. That list is NOT duplicated into SQL — two copies of one
-- vocabulary drift, and the schema's job here is the structural guarantee, not
-- the judgement call.

-- ---------------------------------------------------------------------------
-- 1. Who approved this lesson, and when
-- ---------------------------------------------------------------------------
-- Nullable, because every existing row predates approval and the overwhelming
-- majority of rows are candidates that will never be approved at all.
ALTER TABLE distill_lessons ADD COLUMN approved_by TEXT;
ALTER TABLE distill_lessons ADD COLUMN approved_at TEXT;

-- The review queue is "candidates, best-corroborated first". Partial index
-- because approved rows and retired rows are not what the reviewer opens.
CREATE INDEX IF NOT EXISTS idx_distill_lessons_review
    ON distill_lessons(status, corroboration_count DESC)
    WHERE status = 'candidate';

-- ---------------------------------------------------------------------------
-- 2. The gate
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS distill_lessons_active_requires_approval
BEFORE INSERT ON distill_lessons
WHEN NEW.status = 'active'
 AND NOT (NEW.approved_by IS NOT NULL
          AND TRIM(NEW.approved_by) != ''
          AND NEW.approved_at IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'distill_lessons: an active lesson needs approved_by and approved_at. Corroboration across a cluster is necessary and not sufficient — a mined lesson becomes a rule the owner lives under, so a person approves it first. Route through okuro.sense.distill.lessons.approve_lesson(). Retirement needs no approval and is unaffected.');
END;

-- The UPDATE half, and it is the one that matters. Without it the INSERT guard
-- is decoration: write a candidate, then UPDATE it to active. That is precisely
-- the dodge 137 had to close for verdicts, and 138 for target_ref.
CREATE TRIGGER IF NOT EXISTS distill_lessons_active_requires_approval_upd
BEFORE UPDATE ON distill_lessons
WHEN NEW.status = 'active'
 AND NOT (NEW.approved_by IS NOT NULL
          AND TRIM(NEW.approved_by) != ''
          AND NEW.approved_at IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'distill_lessons: an active lesson needs approved_by and approved_at — see the INSERT trigger. A candidate cannot be promoted by UPDATE without them.');
END;
