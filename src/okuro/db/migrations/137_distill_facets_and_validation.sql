-- <!-- AGENT_HEADER
-- role: code
-- purpose: 137_distill_facets_and_validation — the corpus map, the human calibration set, and a trigger that keeps tier-2 dormant until it exists.
-- index: content
-- AGENT_HEADER_END -->
--
-- Phase 2 of distill-then-delete. Migration 136 built the LEDGER (what a
-- distillation concluded) and the GUARD (nothing leaves agent_events without
-- one). This migration builds the two things that stand between a raw
-- transcript and a ledger row:
--
--   1. distill_facets — one row per session, holding what the cheap tiers
--      derived. Tier-0 (deterministic, free) and tier-1 (one small model call
--      plus an embedding) both write here. This table is NOT the ledger and
--      does not license deleting anything.
--
--   2. distill_validation_batches / _labels — a calibration set the owner
--      labels by hand, and the trigger below makes it the precondition for
--      every tier-2 verdict.
--
-- ---------------------------------------------------------------------------
-- Why the facets are a separate table from session_distillations
-- ---------------------------------------------------------------------------
-- A session_distillations row is load-bearing: gate.py reads its existence,
-- at a trusted rubric_version, as the standing constraint's precondition for
-- DELETING the transcript. So the ledger must mean "this transcript has been
-- distilled and what mattered was retained" and nothing weaker.
--
-- Tier-0 cannot honestly claim that. It counts tool calls and turns; it has
-- not read the session. If tier-0 wrote a ledger row per session, then the
-- day the owner flips deletion_enabled the whole 10k corpus would be deletable
-- on the strength of a row count. The cheap tiers therefore land HERE, and
-- the only tier-0 output that reaches the ledger is the one verdict tier-0 can
-- support from counting alone: `unusable` — an empty stub with no tool calls
-- and nothing said. That rule lives in Python (triage.py) and is pinned by
-- tests/sense/distill/test_tier0_triage.py.
--
-- ---------------------------------------------------------------------------
-- scanned_through_ord, and why it is here rather than a boolean
-- ---------------------------------------------------------------------------
-- Same lesson as migration 135, which was written for the same corpus.
-- Claude Code RESUMES a transcript: trace-ingest appends new agent_events rows
-- to a session distilled weeks ago, and those rows carry their ORIGINAL
-- timestamps, so the session does not look new by any date column. A facet row
-- that recorded only "distilled: yes" would silently describe a prefix of the
-- session and there would be no way to tell from the row.
--
-- So the row records HOW FAR it saw. Default -1 rather than 0 because ord is
-- 0-based and 0 is a real position (the first event).
--
-- ---------------------------------------------------------------------------
-- bodies_available: a facet derived AFTER compaction is a weaker claim
-- ---------------------------------------------------------------------------
-- lifecycle.compact_traces nulls tool_result bodies at the cool tier and more
-- at cold. A facet extracted from a compacted session is built on a skeleton,
-- and nothing in the row would otherwise say so. The distiller is a NEW reader
-- of raw text and the extractor registry (migration 134) does not gate it —
-- deliberately, because registering it would make the compactor wait on the
-- whole 10k backlog being distilled first. This column is the honest
-- alternative: the fidelity of the source is recorded per row, so a later
-- consumer can prefer facets taken while the bodies were still there.
--
-- ---------------------------------------------------------------------------
-- Token mass is NOT agent_sessions.tokens_in/tokens_out
-- ---------------------------------------------------------------------------
-- Measured on the live store 2026-08-13, and it is why this table carries its
-- own token columns rather than joining. Claude Code emits ONE agent_events
-- row per content block, and every row of a message repeats that message's
-- usage figures verbatim:
--
--     msg_011Cdgi4muAkEEe9YL  ords 29,30,31,33  tokens_in=69988 tokens_out=384
--                                               (the SAME numbers, four times)
--
-- agent_sessions.tokens_out is `SELECT SUM(tokens_out) FROM agent_events`
-- (claude_code.py:318), so it multiplies output by the blocks-per-message
-- factor — 2.2x to 3.0x across the six largest claude-code sessions measured.
-- tokens_in is worse than inflated: it is the CUMULATIVE context size at each
-- message, so summing it is not a quantity of anything.
--
-- CODEX IS FAR WORSE, and it is a different shape rather than more of the same.
-- Measured live 2026-08-13: codex carries usage on `progress` rows, not
-- assistant rows, and those figures are RUNNING TOTALS that are also duplicated
-- across rows. On session 019c4a4f the 1535 values are monotonically
-- non-decreasing from 551 to 389 244, so:
--
--     MAX(tokens_out)  =         389 244   <- the session's actual output
--     SUM(tokens_out)  =     305 182 044   <- what agent_sessions records
--                                             784x the truth
--
-- Two provider shapes, therefore, and facets.py reads whichever one the
-- session actually has. gemini and antigravity carry no usage on any row type,
-- which is why these columns are nullable.

-- ---------------------------------------------------------------------------
-- 1. The corpus map
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS distill_facets (
    session_id            TEXT PRIMARY KEY,   -- agent_sessions.session_id (native)

    -- Bumped when extraction semantics change; a row below the current version
    -- is re-derivable and the pipeline re-does it. Same contract as
    -- interaction_scanned.detector_version.
    extraction_version    INTEGER NOT NULL,
    -- Highest agent_events.ord this row saw. See the header.
    scanned_through_ord   INTEGER NOT NULL DEFAULT -1,

    -- How far the pipeline got. tier1 implies tier0 ran first.
    stage                 TEXT NOT NULL DEFAULT 'tier0'
                          CHECK (stage IN ('tier0', 'tier1')),

    -- Subagent runs are ~half the corpus and are a different population:
    -- their type='user' turn is a parent agent's brief, not a human message.
    -- Read off the session id shape, the same structural signal
    -- interaction.turns.session_kind uses.
    session_class         TEXT NOT NULL
                          CHECK (session_class IN ('main', 'subagent')),

    -- Tier-0's verdict about where this session GOES, which is not the same as
    -- a verdict about the session. 'unusable' is the only one tier-0 can reach
    -- from counting; everything else routes to the judge, including pure
    -- conversation with zero tool calls — that is the richest material in the
    -- corpus, not the emptiest.
    triage                TEXT NOT NULL
                          CHECK (triage IN ('unusable', 'judge')),

    -- ---- deterministic signals, no model call ----------------------------
    tool_calls            INTEGER NOT NULL DEFAULT 0,
    user_turns            INTEGER NOT NULL DEFAULT 0,
    -- DISTINCT assistant message ids, not assistant ROWS. One message is 1-4
    -- rows (thinking / text / tool_use each land separately), so a row count
    -- overstates turns by 1.7x-2.5x across the six largest sessions measured
    -- live 2026-08-13.
    assistant_messages    INTEGER NOT NULL DEFAULT 0,
    -- ALL THREE NULLABLE, and the distinction is the same one the dimension
    -- scores make in 136: NULL means the provider reports no usage at all,
    -- 0 would mean it reported zero. gemini (167 sessions) and antigravity
    -- (105) carry token data on no row type whatsoever — measured live
    -- 2026-08-13 — so NOT NULL DEFAULT 0 would state, in the corpus map, that
    -- 272 sessions produced no output.
    tokens_out_total      INTEGER,

    -- The two input figures are SEPARATE COLUMNS because the two providers do
    -- not report the same quantity, and one column holding both would be a
    -- column with two meanings.
    --
    --   context_peak      the largest context a single message carried.
    --                     claude-code only; NULL for codex.
    --   tokens_in_total   total input consumed across the session.
    --                     codex only; NULL for claude-code.
    --
    -- Conflating them was tried and produced a codex session claiming a
    -- 108 642 435-token context — the max of a running total of input
    -- CONSUMPTION, read as if it were a context size.
    context_peak          INTEGER,
    tokens_in_total       INTEGER,

    -- Which shape the figures above came in: 'per_message' | 'cumulative' |
    -- NULL. A reader comparing token mass across providers needs to know it is
    -- not comparing like with like.
    token_shape           TEXT
                          CHECK (token_shape IS NULL
                                 OR token_shape IN ('per_message', 'cumulative')),
    distinct_tools        TEXT NOT NULL DEFAULT '[]',   -- JSON array of tool names
    provider              TEXT,

    -- Signals that already exist elsewhere, denormalised so the corpus map is
    -- one read. compliance_normalized is NULL for the ~90% of sessions with no
    -- session_bridge link — NULL means unknown, never 0.
    compliance_normalized REAL,
    markers               TEXT NOT NULL DEFAULT '[]',   -- JSON array of detector ids
    marker_count          INTEGER NOT NULL DEFAULT 0,

    -- Sampling policy input: 100% of flagged sessions reach the judge.
    flagged               INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    flag_reasons          TEXT NOT NULL DEFAULT '[]',   -- JSON array

    -- 0 when the session was compacted before this row was derived. See header.
    bodies_available      INTEGER NOT NULL DEFAULT 1
                          CHECK (bodies_available IN (0, 1)),

    -- ---- tier-1 ----------------------------------------------------------
    -- The facet document: task type, outcome signal, error/frustration
    -- markers, in the shape corpus.py::FACET_KEYS declares. NULL until tier-1
    -- runs. Length-CHECKed for the same reason evidence_quotes is in 136: a
    -- summary field with no ceiling becomes a second copy of the transcript
    -- and defeats the point of deleting the first one.
    facet                 TEXT CHECK (facet IS NULL OR LENGTH(facet) <= 4096),
    facet_model           TEXT,
    -- CHARACTERS, and the column is named for what it holds. Neither bridge
    -- path returns token usage — CLI providers parse text and the local-http
    -- path drops the OpenAI `usage` block (bridge/local.py:124) — so a column
    -- called facet_tokens would have held an estimate under a name that reads
    -- like a measurement. Callers convert at a declared ratio and say so.
    facet_chars           INTEGER,
    cluster_id            INTEGER,

    -- Resume-safety input: the pipeline skips sessions still receiving events.
    last_event_ts         TEXT,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_distill_facets_stage
    ON distill_facets(stage, extraction_version);
CREATE INDEX IF NOT EXISTS idx_distill_facets_triage
    ON distill_facets(triage);
CREATE INDEX IF NOT EXISTS idx_distill_facets_cluster
    ON distill_facets(cluster_id) WHERE cluster_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_distill_facets_flagged
    ON distill_facets(flagged) WHERE flagged = 1;

-- vec_distill_sessions is created post-migrate by ensure_vec_dims() at the
-- active embedding tier dim, same as every other vec_* table (see
-- embed/repair.py SPECS). Session-level and UNPARTITIONED: ~10k vectors is one
-- chunk-block, and a partition key costs 4 MiB of preallocation per distinct
-- value — which for a session_id partition would be 4 MiB per session.

-- ---------------------------------------------------------------------------
-- 2. The human calibration set
-- ---------------------------------------------------------------------------
-- An LLM judge is only worth its verdicts if somebody measured it against
-- ground truth. The owner labels a sample by hand; that sample is a batch; a
-- tier-2 verdict must name the batch it was calibrated against.
--
-- This is not bookkeeping — it is the switch that keeps tier-2 dormant. Until
-- an APPROVED batch of sufficient size exists, the trigger in section 3 makes
-- a tier-2 row impossible to write, from Python and from the sqlite3 shell
-- alike.
--
-- WHAT "≥ 1 LABEL" LET THROUGH, AND WHY THE BAR MOVED
-- The first version of this gate asked only whether the named batch held any
-- label row at all. A reviewer landed a fully judged verdict against it in
-- three statements: create a batch, insert ONE label — for an unrelated
-- session, authored by a model — then write the verdict. Every individual
-- check passed and the thing they were supposed to compose into, a measured
-- human calibration, never happened.
--
-- So the batch now has to clear three things at once, and each closes a
-- different cheap path:
--
--   approved_at / approved_by   approval is a SECOND, deliberate act. Creating
--                               a batch is now not the same as blessing it.
--   min_validation_labels       a floor, in distill_config so the owner can tune
--                               it without a migration. 50 hand labels is real
--                               work; one is a rounding error.
--   PK (batch_id, session_id)   already there, and load-bearing here: it makes
--                               the floor count DISTINCT SESSIONS, so 50 rows
--                               cannot be one session labelled fifty times.
--
-- WHAT THIS HONESTLY DOES NOT DO. SQL cannot verify that a human typed the
-- labels — `approved_by` is a string, and any writer can set it. What it CAN
-- do is make every CHEAP forgery impossible and leave only a path expensive
-- enough that nobody walks it by accident: fifty distinct labelled sessions
-- plus an explicit approval. That is the real boundary, and stating it here is
-- better than implying a guarantee the schema cannot give.
CREATE TABLE IF NOT EXISTS distill_validation_batches (
    batch_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    rubric_version  INTEGER NOT NULL,
    -- Who produced the labels. A batch labelled by a model is not a
    -- calibration set, it is the thing being calibrated.
    labelled_by     TEXT NOT NULL,
    -- Approval — NULL until somebody blesses the batch. Both must be set, and
    -- omitting either fails CLOSED, which is the correct direction: the 136
    -- lesson is that a guard keyed on a column being PRESENT is dodged by
    -- omitting it, so this guard is keyed on columns being ABSENT.
    approved_at     TEXT,
    approved_by     TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS distill_validation_labels (
    batch_id     TEXT NOT NULL
                 REFERENCES distill_validation_batches(batch_id) ON DELETE CASCADE,
    session_id   TEXT NOT NULL,
    verdict      TEXT NOT NULL
                 CHECK (verdict IN ('good', 'bad', 'mixed', 'unusable')),

    -- Same four dimensions and the same [0,1] REAL / NULL-means-not-assessed
    -- contract as session_distillations, so a label and a verdict compare
    -- directly without a scale conversion.
    score_protocol_compliance     REAL CHECK (score_protocol_compliance     IS NULL OR (score_protocol_compliance     BETWEEN 0.0 AND 1.0)),
    score_task_outcome            REAL CHECK (score_task_outcome            IS NULL OR (score_task_outcome            BETWEEN 0.0 AND 1.0)),
    score_communication_contract  REAL CHECK (score_communication_contract  IS NULL OR (score_communication_contract  BETWEEN 0.0 AND 1.0)),
    score_tool_routing            REAL CHECK (score_tool_routing            IS NULL OR (score_tool_routing            BETWEEN 0.0 AND 1.0)),

    note         TEXT,
    labelled_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (batch_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_distill_validation_labels_session
    ON distill_validation_labels(session_id);

-- The floor, on the config row rather than in the trigger body, because a
-- number baked into a trigger can only be changed by a migration and this one
-- is a judgement call the owner owns. 50 is the plan's figure.
--
-- Read with COALESCE(..., 1000000000) at every use: a store whose config row
-- is missing must refuse EVERY batch, not accept every batch, and a bare
-- subquery returning NULL would make the comparison NULL and the guard
-- fail open — the same shape as 136's `IS NOT 1`.
ALTER TABLE distill_config
    ADD COLUMN min_validation_labels INTEGER NOT NULL DEFAULT 50;

-- ---------------------------------------------------------------------------
-- 3. The refusal — tier-2 cannot write a verdict without a labelled batch
-- ---------------------------------------------------------------------------
-- Same reasoning as 136's guards: a precondition enforced by the caller
-- protects one call site, and this store is reachable from a daemon task, a
-- CLI, a future agent and the sqlite3 binary. So the check moves into the
-- schema, where every writer meets it.
--
-- WHAT COUNTS AS A TIER-0 ROW, and why the condition is spelled positively.
-- The obvious spelling is "if this looks like a judge verdict, demand a
-- batch" — keyed on judge_model being set. That FAILS OPEN in the one
-- direction that matters: a writer that omits judge_model gets a free pass,
-- and omitting a column is the easiest mistake in the file. So the trigger
-- inverts it. A row is exempt only when it matches the tier-0 shape EXACTLY:
--
--     verdict = 'unusable'   (the only verdict counting alone can support)
--     judge_model IS NULL
--     all four dimension scores IS NULL   (tier-0 assesses no dimension)
--
-- Anything else — any verdict, any score, any judge — is a tier-2 row and
-- must name a batch that actually holds labels. Every term is NULL-total:
-- `IS NULL` never yields NULL, verdict is NOT NULL by its own column
-- constraint, and the EXISTS is a boolean. That is the lesson from 136's
-- `IS NOT 1`: the one input meaning "something is wrong here" must not be the
-- input that unlocks the gate.
--
-- WHAT THE BATCH MUST CLEAR, and why "holds a label" was not enough. The
-- first version asked only `EXISTS (SELECT 1 FROM labels WHERE batch_id=…)`.
-- A reviewer defeated it in three statements: create a batch, insert ONE
-- model-authored label for an UNRELATED session, write the verdict. So the
-- predicate now demands all three of:
--
--     approved_at IS NOT NULL AND approved_by IS NOT NULL
--         approval is a second deliberate act, and omitting either column
--         refuses — keyed on ABSENCE, which is the fail-closed direction.
--     COUNT(labels) >= distill_config.min_validation_labels
--         50 by default. The labels PK is (batch_id, session_id), so this
--         counts DISTINCT SESSIONS: fifty rows cannot be one session fifty
--         times.
--     COALESCE(floor, 1000000000)
--         a store with no config row refuses every batch rather than
--         accepting every batch.
--
-- Counting the labels rather than trusting a counter column stays deliberate:
-- a batch row claiming label_count=50 with nothing behind it is exactly the
-- shape being prevented, and a count over the real rows cannot desynchronise.
--
-- The whole clause is one NOT EXISTS, which is 0/1 and never NULL — so no
-- input can make the WHEN clause evaluate NULL and let the write through.
--
-- Not covered, and not coverable in SQL: DROP TRIGGER. judge.py checks the
-- trigger is present in sqlite_master before it will run, the same runtime
-- answer gate.py gives for the same hole.
CREATE TRIGGER IF NOT EXISTS session_distillations_require_validated_batch
BEFORE INSERT ON session_distillations
WHEN NOT (NEW.verdict = 'unusable'
          AND NEW.judge_model IS NULL
          AND NEW.score_protocol_compliance    IS NULL
          AND NEW.score_task_outcome           IS NULL
          AND NEW.score_communication_contract IS NULL
          AND NEW.score_tool_routing           IS NULL)
 AND NOT EXISTS (
        SELECT 1 FROM distill_validation_batches b
        WHERE b.batch_id    = NEW.validation_batch_id
          AND b.approved_at IS NOT NULL
          AND b.approved_by IS NOT NULL
          AND (SELECT COUNT(*) FROM distill_validation_labels l
               WHERE l.batch_id = b.batch_id)
              >= COALESCE((SELECT min_validation_labels FROM distill_config
                           WHERE id = 1), 1000000000)
     )
BEGIN
    SELECT RAISE(ABORT, 'session_distillations: a judged verdict needs validation_batch_id naming an APPROVED distill_validation_batches row (approved_at and approved_by both set) holding at least distill_config.min_validation_labels distinct labelled sessions. Tier-2 stays dormant until the owner has labelled AND approved a calibration set. Tier-0 may write only verdict=unusable with no scores and no judge_model.');
END;

-- The UPDATE half. Without it the refusal is a formality: insert the tier-0
-- shape, then UPDATE it into a judged verdict. Same condition, reading NEW.
CREATE TRIGGER IF NOT EXISTS session_distillations_require_validated_batch_upd
BEFORE UPDATE ON session_distillations
WHEN NOT (NEW.verdict = 'unusable'
          AND NEW.judge_model IS NULL
          AND NEW.score_protocol_compliance    IS NULL
          AND NEW.score_task_outcome           IS NULL
          AND NEW.score_communication_contract IS NULL
          AND NEW.score_tool_routing           IS NULL)
 AND NOT EXISTS (
        SELECT 1 FROM distill_validation_batches b
        WHERE b.batch_id    = NEW.validation_batch_id
          AND b.approved_at IS NOT NULL
          AND b.approved_by IS NOT NULL
          AND (SELECT COUNT(*) FROM distill_validation_labels l
               WHERE l.batch_id = b.batch_id)
              >= COALESCE((SELECT min_validation_labels FROM distill_config
                           WHERE id = 1), 1000000000)
     )
BEGIN
    SELECT RAISE(ABORT, 'session_distillations: a judged verdict needs validation_batch_id naming an APPROVED batch holding at least distill_config.min_validation_labels distinct labelled sessions — see the INSERT trigger.');
END;

-- ---------------------------------------------------------------------------
-- 4. The tombstone guard — protecting the record of unrecoverable loss
-- ---------------------------------------------------------------------------
-- A distill_tombstones row says: this session's events were deleted, and the
-- ONLY surviving copy is the archive at this path with this digest. Delete the
-- tombstone and the archive becomes an orphan file — still on disk, but with
-- nothing left to say which session it holds, whether its digest still
-- matches, or that a deletion ever happened. The events are already gone by
-- then, so this is the one table in the set whose loss cannot be repaired by
-- re-deriving anything.
--
-- Reuses okuro_distill_gate_armed() rather than minting a third sentinel. The
-- semantics line up exactly — tombstone removal is a retention-gate-class act
-- — and the function is already registered on every okuro connection by
-- db/sqlite.py::_make_conn, so the guard needs no new plumbing and a sqlite3
-- shell still cannot compile a DELETE against this table at all.
--
-- Nothing in okuro deletes a tombstone. gate.py only ever INSERTs one, under
-- the same sentinel, so the legitimate path is already inside the arming
-- window and this trigger is a no-op for it.
--
-- WHY session_distillations AND distill_validation_* ARE DELIBERATELY NOT
-- GUARDED. They fail SAFE. Deleting a ledger row removes the precondition
-- gate.py requires, so the session simply becomes undeletable again —
-- `not_distilled`, counted and refused. Deleting a validation batch or its
-- labels re-locks tier-2 by the trigger above. In both cases the damage from a
-- delete is that LESS is permitted, never more, and a guard whose only effect
-- is to make a self-correcting action harder is noise. The tombstone is the
-- opposite: its loss permits nothing, it destroys the only remaining pointer
-- to data that no longer exists anywhere else.
CREATE TRIGGER IF NOT EXISTS distill_tombstones_delete_guard
BEFORE DELETE ON distill_tombstones
WHEN okuro_distill_gate_armed() IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'distill_tombstones is delete-protected — a tombstone is the only record of which archive holds a deleted session, and its events are already gone. Route any removal through okuro.sense.distill.gate.gate_armed().');
END;
