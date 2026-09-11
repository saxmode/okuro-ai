-- <!-- AGENT_HEADER
-- role: code
-- purpose: 138_distill_lessons_phase3 — what a mined lesson must name before it can exist, and the evidence table that makes its counters idempotent.
-- index: content
-- AGENT_HEADER_END -->
--
-- Phase 3 of distill-then-delete. Migration 136 built the LEDGER and the
-- retention GUARD; 137 built the CORPUS MAP and kept tier-2 dormant. This
-- migration closes the loop: cluster-level defect mining writes lessons, and
-- active lessons become proposals on okuro's EXISTING improvement lifecycle.
--
-- Two halves, and both are guards rather than storage:
--
--   1. A lesson must name WHERE it applies, from a CLOSED vocabulary. Free
--      text here is the defect that already happened once (see below).
--   2. A lesson's ACE counters must be idempotent, so re-running maintenance
--      cannot inflate the evidence a retirement decision is made on.
--
-- ---------------------------------------------------------------------------
-- Why target_ref is a closed vocabulary and not a string
-- ---------------------------------------------------------------------------
-- The binding architecture decision (the owner, 2026-07-26) is "data is
-- personal, mechanism is general": okuro's self-improvement machinery must
-- carry no user-specific content. `interaction_improvements.target_ref` is a
-- bare TEXT column filled by a model, and the measured consequence was that
-- USER PROJECT NAMES leaked into it — a personal datum sitting in the general
-- mechanism, from a column whose type permitted anything.
--
-- The lesson pipeline is a second writer against that same surface, driven by
-- an even cheaper source (a cluster summary, not a reviewed proposal), so it
-- gets the constraint the original column never had. The vocabulary below is
-- every okuro-internal location a lesson may address. Nothing else is
-- representable — not by mining.py, not by a future agent, not from the
-- sqlite3 shell.
--
-- WHY IT IS A TRIGGER AND NOT A CHECK. Two reasons, and the second is the real
-- one. SQLite's ALTER TABLE ADD COLUMN accepts a CHECK, but a CHECK cannot run
-- a subquery — and `role.pitfall:<role_id>` has to be validated against the
-- roles that actually exist, otherwise the "closed" vocabulary is closed only
-- at the prefix and the suffix is free text again. That is the same hole in
-- miniature: a guard that constrains the shape of a string while leaving its
-- content arbitrary. Migration 136 put its checks in the schema so every
-- writer meets them; this follows that doctrine.
--
-- FAIL-CLOSED SPELLING, same lesson as 136's `IS NOT 1`. The predicate is
-- written as "abort unless the value is one of these", never "abort if the
-- value looks wrong". A NULL target_ref is explicitly permitted (a lesson may
-- be surface-wide), and every other term is NULL-total, so no input can make
-- the WHEN clause evaluate NULL and slip through.
--
-- ---------------------------------------------------------------------------
-- Why the ACE counters need their own evidence table
-- ---------------------------------------------------------------------------
-- helpful_count / harmful_count decide retirement. If maintenance is run twice
-- over the same window — a daemon retry, a manual invocation, a re-run after a
-- crash — a bare `UPDATE ... SET helpful_count = helpful_count + 1` counts the
-- same session again, and a lesson is retired or kept on evidence that was
-- manufactured by the scheduler rather than observed in the corpus.
--
-- So the counters are DERIVED from rows, not accumulated in place, and the
-- PRIMARY KEY (lesson_id, session_id) makes a second observation of the same
-- session a no-op at the schema level. The same shape migration 137 used to
-- make its label floor count DISTINCT SESSIONS rather than rows.
--
-- The verdict column records what the session showed, in the pipeline's own
-- vocabulary, and 'against' is named for what it is: a post-activation session
-- in the lesson's cluster that STILL exhibits the defect. That is evidence the
-- lesson did not work. It is NOT evidence the lesson caused harm — nothing
-- here can establish that, and calling the column `harmful` in 136 has to be
-- read with this caveat rather than at face value.

-- ---------------------------------------------------------------------------
-- 1. What a lesson must carry to be actionable
-- ---------------------------------------------------------------------------
-- Every column added here is nullable or defaulted: migration 136 shipped
-- distill_lessons and Phase 1/2 may already have written rows, so the ALTERs
-- must not invalidate them.

-- Which cluster corroborated this lesson. The re-distillation selector reads
-- it (rubric.py::sessions_needing_redistillation) to find the sessions a
-- rubric change implicates, so it is the join key between a lesson and the
-- population that produced it.
ALTER TABLE distill_lessons ADD COLUMN cluster_id INTEGER;

-- The rubric in force when this lesson was mined. A lesson is only as good as
-- the definition of "good" it was derived under; without this, a rubric bump
-- silently leaves old lessons looking current.
ALTER TABLE distill_lessons ADD COLUMN rubric_version INTEGER NOT NULL DEFAULT 1;

-- The okuro surface a promoted lesson would change. Deliberately the SAME
-- seven-value vocabulary as interaction_improvements.surface (migration 114) —
-- a lesson that cannot name an existing surface has nowhere to go, and adding
-- an eighth value here would be the "new injection channel" the architecture
-- decision forbids.
ALTER TABLE distill_lessons ADD COLUMN target_surface TEXT;

-- WHERE on that surface. Closed vocabulary, enforced below.
ALTER TABLE distill_lessons ADD COLUMN target_ref TEXT;

-- JSON array of interaction_markers ids this lesson is MEASURED by. Not
-- decoration: it is what interaction_improvements.baseline_value is computed
-- from, and what the ACE counters read to decide whether the defect recurred.
-- A lesson with no markers can be proposed but can never be verified, so
-- mining refuses to emit one.
ALTER TABLE distill_lessons ADD COLUMN markers TEXT NOT NULL DEFAULT '[]';

-- Distinct sessions in the cluster that asserted this lesson's defect. One
-- session asserts a CANDIDATE; this count crossing the configured threshold is
-- what makes it a FINDING.
ALTER TABLE distill_lessons ADD COLUMN corroboration_count INTEGER NOT NULL DEFAULT 0;

-- The interaction_improvements row this lesson emitted, once active. NULL
-- means "not yet proposed". One lesson emits at most one improvement — the
-- uniqueness is enforced by the index below rather than by the writer, because
-- a duplicate here means the same advice reaches the owner twice.
ALTER TABLE distill_lessons ADD COLUMN improvement_id TEXT;

-- Hysteresis inputs. status_changed_at is the dwell clock: a lesson may not
-- change status again until it has held the current one long enough, which is
-- what stops candidate->active->retired->active flapping as evidence trickles
-- in. last_evidence_at is the staleness clock.
ALTER TABLE distill_lessons ADD COLUMN status_changed_at TEXT;
ALTER TABLE distill_lessons ADD COLUMN last_evidence_at TEXT;
ALTER TABLE distill_lessons ADD COLUMN retired_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_distill_lessons_cluster
    ON distill_lessons(cluster_id) WHERE cluster_id IS NOT NULL;

-- One improvement per lesson. A partial UNIQUE index rather than a column
-- constraint because the overwhelming majority of rows are NULL here and
-- SQLite treats NULLs as distinct, which is exactly the behaviour wanted.
CREATE UNIQUE INDEX IF NOT EXISTS idx_distill_lessons_improvement
    ON distill_lessons(improvement_id) WHERE improvement_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. The evidence table behind the ACE counters
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS distill_lesson_evidence (
    lesson_id    INTEGER NOT NULL
                 REFERENCES distill_lessons(id) ON DELETE CASCADE,
    session_id   TEXT NOT NULL,
    -- 'for'     — post-activation cluster session free of the lesson's markers
    -- 'against' — post-activation cluster session still exhibiting them
    -- 'evidence'— a session that asserted the defect at mining time
    verdict      TEXT NOT NULL CHECK (verdict IN ('for', 'against', 'evidence')),
    observed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lesson_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_distill_lesson_evidence_verdict
    ON distill_lesson_evidence(lesson_id, verdict);

-- ---------------------------------------------------------------------------
-- 3. The closed target vocabulary
-- ---------------------------------------------------------------------------
-- Spelled once, in a trigger body, for INSERT and UPDATE alike. The UPDATE half
-- is not optional: without it a writer inserts a valid row and then updates
-- target_ref into anything, which is the exact hole 137 closed for verdicts.
--
-- `role.pitfall:<role_id>` is the only parameterised member, and its suffix is
-- checked against the roles table — a role that does not exist is not a target,
-- and permitting an arbitrary suffix would reopen the free-text hole through
-- the one member that has a parameter.
CREATE TRIGGER IF NOT EXISTS distill_lessons_target_vocabulary
BEFORE INSERT ON distill_lessons
WHEN NOT (
        NEW.target_ref IS NULL
     OR NEW.target_ref IN (
            'bootstrap.failure_modes',
            'bootstrap.conventions',
            'bootstrap.behavioral_contract',
            'bootstrap.composition',
            'tool_protocol.routing',
            'tool_protocol.deliverables',
            'tool_protocol.system_state',
            'subagent_brief.template',
            'principle_set',
            'mcp_middleware.gate',
            'memory.lifecycle'
        )
     OR (NEW.target_ref LIKE 'role.pitfall:%'
         AND EXISTS (SELECT 1 FROM roles
                      WHERE role_id = substr(NEW.target_ref, 14)))
    )
BEGIN
    SELECT RAISE(ABORT, 'distill_lessons.target_ref must name an okuro-internal location from the closed vocabulary (bootstrap.*, tool_protocol.*, subagent_brief.template, principle_set, mcp_middleware.gate, memory.lifecycle, or role.pitfall:<existing role_id>). Free text here leaks user-specific data into a general mechanism — see okuro.sense.distill.lessons.LESSON_TARGETS.');
END;

CREATE TRIGGER IF NOT EXISTS distill_lessons_target_vocabulary_upd
BEFORE UPDATE ON distill_lessons
WHEN NOT (
        NEW.target_ref IS NULL
     OR NEW.target_ref IN (
            'bootstrap.failure_modes',
            'bootstrap.conventions',
            'bootstrap.behavioral_contract',
            'bootstrap.composition',
            'tool_protocol.routing',
            'tool_protocol.deliverables',
            'tool_protocol.system_state',
            'subagent_brief.template',
            'principle_set',
            'mcp_middleware.gate',
            'memory.lifecycle'
        )
     OR (NEW.target_ref LIKE 'role.pitfall:%'
         AND EXISTS (SELECT 1 FROM roles
                      WHERE role_id = substr(NEW.target_ref, 14)))
    )
BEGIN
    SELECT RAISE(ABORT, 'distill_lessons.target_ref must name an okuro-internal location from the closed vocabulary — see the INSERT trigger.');
END;

-- ---------------------------------------------------------------------------
-- 4. The surface vocabulary
-- ---------------------------------------------------------------------------
-- Same seven values as interaction_improvements.surface. Enforced here too
-- because a lesson carrying an unroutable surface would be written happily by
-- mining and then fail at emission time — one table away from the writer that
-- got it wrong, which is where debugging gets expensive.
CREATE TRIGGER IF NOT EXISTS distill_lessons_surface_vocabulary
BEFORE INSERT ON distill_lessons
WHEN NOT (NEW.target_surface IS NULL
          OR NEW.target_surface IN ('subagent_brief', 'enforcement_hook',
                                    'role', 'user_profile', 'principle',
                                    'bootstrap', 'memory_hygiene'))
BEGIN
    SELECT RAISE(ABORT, 'distill_lessons.target_surface must be one of interaction_improvements'' seven surfaces — a lesson targeting anything else has no existing lifecycle to flow into, and adding an eighth is the new injection channel the architecture decision forbids.');
END;

CREATE TRIGGER IF NOT EXISTS distill_lessons_surface_vocabulary_upd
BEFORE UPDATE ON distill_lessons
WHEN NOT (NEW.target_surface IS NULL
          OR NEW.target_surface IN ('subagent_brief', 'enforcement_hook',
                                    'role', 'user_profile', 'principle',
                                    'bootstrap', 'memory_hygiene'))
BEGIN
    SELECT RAISE(ABORT, 'distill_lessons.target_surface must be one of interaction_improvements'' seven surfaces — see the INSERT trigger.');
END;

-- ---------------------------------------------------------------------------
-- 5. vec_distill_lessons
-- ---------------------------------------------------------------------------
-- Created post-migrate by ensure_vec_dims() at the active embedding tier dim,
-- exactly like vec_distill_sessions (embed/repair.py SPECS). UNPARTITIONED and
-- permanently so: lessons number in the hundreds, the only column anyone would
-- reach for as a partition key is the lesson id itself, and a partition value
-- per lesson preallocates 4 MiB PER LESSON. tests/embed/
-- test_vec_partition_cardinality.py enforces that at the spec level.
