-- <!-- AGENT_HEADER
-- role: code
-- purpose: 136_distill_ledger_and_retention_gate — the qualification ledger, and a delete guard that outranks the code calling it.
-- index: content
-- AGENT_HEADER_END -->
--
-- The class of defect this closes: A DESTRUCTIVE OPERATION WHOSE ONLY
-- PROTECTION IS THAT THE CALLER REMEMBERS TO CHECK.
--
-- A standing constraint (the owner, 2026-07-16) says agent_events must not be
-- deleted until an automated loop has qualified each transcript and that
-- qualification is retained. Every conventional way to honour that — a guard
-- clause, a code review, a comment — protects exactly one call site. The store
-- is reachable from a daemon task, a CLI, a migration, a future agent, and the
-- sqlite3 binary; a convention that lives in one function protects none of
-- them. So the check moves INTO the schema, where every writer meets it.
--
-- Two halves:
--   1. The LEDGER — what a distillation produced, what lessons came out of it,
--      and what was deleted afterwards. Deletion is gated on this existing.
--   2. The GUARD — a BEFORE DELETE trigger on agent_events that aborts unless
--      the connection has armed the gate.
--
-- ---------------------------------------------------------------------------
-- Why the guard is keyed on a FUNCTION and not, as first designed, a temp table
-- ---------------------------------------------------------------------------
-- The intended sentinel was a temp table created only inside the gate's own
-- connection, so no other connection could see it. SQLite rejects that outright:
--
--     sqlite3.OperationalError: trigger <name> cannot reference objects in
--     database temp
--
-- (measured, sqlite 3.45.1). A trigger body may only touch the schema it lives
-- in, which rules out every per-connection object SQLite has EXCEPT one: a
-- user-defined function, registered on a connection at runtime.
--
-- That substitution is strictly stronger than the temp table would have been.
-- A connection that has not registered okuro_distill_gate_armed() does not get
-- a polite refusal, it cannot compile the statement at all:
--
--     sqlite3.OperationalError: no such function: okuro_distill_gate_armed
--
-- The sqlite3 CLI has no way to define one, so the shell path this constraint
-- most needed to cover is closed by construction rather than by policy.
--
-- The alternative considered and rejected was a sentinel ROW written inside the
-- gate's write transaction (invisible to other connections under WAL snapshot
-- isolation). It works, but it makes an unarmed delete fail with "database is
-- locked" — indistinguishable from ordinary contention, which is precisely the
-- confusion the by-reason counters exist to prevent.
--
-- ---------------------------------------------------------------------------
-- Why the WHEN clause says IS NOT 1
-- ---------------------------------------------------------------------------
-- The obvious spelling, `WHEN okuro_distill_gate_armed() != 1`, FAILS OPEN. If
-- the function returns NULL — a bug in the arming code, a wrapper that forgets
-- to return — then `NULL != 1` evaluates to NULL, the WHEN clause is not
-- satisfied, the trigger does not fire, and the delete proceeds. The one input
-- that means "something is wrong with the gate" is the one input that unlocks
-- it. `IS NOT 1` is NULL-total: anything that is not exactly the integer 1
-- aborts. Pinned by a test that arms the gate with a NULL-returning function.
--
-- ---------------------------------------------------------------------------
-- What the guard covers that a code-level check would not
-- ---------------------------------------------------------------------------
-- agent_events.session_id carries ON DELETE CASCADE from agent_sessions
-- (016_trace_store.sql:41). Deleting one agent_sessions row therefore deletes
-- every event under it, without any statement naming agent_events. Measured:
-- a BEFORE DELETE trigger on the child DOES fire for rows removed by a cascade,
-- under foreign_keys=ON and =OFF alike, and its RAISE(ABORT) rolls the parent
-- delete back too. So the cascade is covered rather than a hole — but only
-- because the guard sits on the child. A guard on agent_sessions would have
-- missed every other route in.
--
-- ---------------------------------------------------------------------------
-- The second delete path: REPLACE, which fires no DELETE trigger by default
-- ---------------------------------------------------------------------------
-- A BEFORE DELETE trigger is NOT sufficient on its own, and the first version
-- of this migration shipped believing it was.
--
-- `INSERT OR REPLACE` (and `REPLACE INTO`) resolve a uniqueness conflict by
-- deleting the existing row and inserting the new one. With SQLite's DEFAULT
-- `recursive_triggers=OFF`, that delete fires NO DELETE TRIGGER AT ALL —
-- measured on this schema, the statement fires only BEFORE INSERT and AFTER
-- INSERT. So the guard below was bypassed completely: the original row's body
-- was destroyed while `DELETE FROM agent_events` on the same connection was
-- correctly refused.
--
-- The damage did not stop at the row. Because AFTER DELETE never fired, the
-- FTS sync trigger from 016 never removed the old index entry, and the state
-- that leaves behind is worse than a visibly broken one:
--
--     INSERT INTO agent_events_fts(agent_events_fts) VALUES('integrity-check')
--         -> PASSES
--     INSERT INTO agent_events_fts(agent_events_fts, rank)
--         VALUES('integrity-check', 1)
--         -> sqlite3.DatabaseError: database disk image is malformed
--     SELECT text  FROM agent_events_fts WHERE agent_events_fts MATCH 'body'
--         -> sqlite3.DatabaseError: database disk image is malformed
--     SELECT rowid FROM agent_events_fts WHERE agent_events_fts MATCH 'body'
--         -> [{'rowid': 1}]   a hit on a row that no longer exists
--     SELECT COUNT(*) FROM agent_events_fts
--         -> 4, matching agent_events exactly
--
-- So the bare integrity-check verifies the index against ITSELF and is NOT
-- proof the index is readable; only the `rank = 1` form compares it against
-- the content table. The gate uses rank=1 for exactly that reason. The column
-- asymmetry is the same trap in miniature: an external-content table resolves
-- values THROUGH the content table, so a probe selecting rowid alone looks
-- like a check and verifies nothing.
--
-- WHAT WAS TRIED FIRST AND REVERTED. `PRAGMA recursive_triggers=ON` does make
-- REPLACE fire the DELETE triggers, and it was committed on that basis after a
-- scan reported the schema free of self-referencing triggers. That scan read
-- trigger HEADERS, not BODIES, and it was wrong. Two triggers UPDATE the table
-- they fire on:
--
--     artifacts_touch_updated_at
--     knowledge_saved_queries_touch_updated_at
--
-- both `AFTER UPDATE ON t BEGIN UPDATE t SET updated_at = ... END`. With the
-- pragma on they recurse to the depth limit: 16 failures and 3 errors across
-- artifacts, memory and orchestrator. Fixing them means rewriting two
-- unrelated subsystems' triggers to `AFTER UPDATE OF <columns>`, which is a
-- real option but not one to take silently from inside a retention change.
--
-- So the close does not use the pragma. `agent_events_replace_guard` (below)
-- is a BEFORE INSERT trigger that refuses when the uuid ALREADY EXISTS and the
-- connection has not armed `okuro_trace_upsert_armed()`. REPLACE and upsert
-- are indistinguishable at BEFORE INSERT time — measured, a guard keyed only
-- on "uuid exists" blocks both — so the arming function is what separates
-- them: the four trace ingesters arm it around their legitimate
-- `ON CONFLICT(uuid) DO UPDATE`, and nothing else does.
--
-- That covers both directions at once. An okuro caller that REPLACEs without
-- arming is refused by the schema, not by convention. A raw sqlite3 shell
-- cannot define the function, so no statement it issues against agent_events
-- compiles — which is the stronger answer for the shell than the pragma could
-- have given, since a shell sets its own pragmas and would have left
-- recursive_triggers OFF anyway.
--
-- Not covered, and not coverable from here: DROP TABLE, DROP TRIGGER, and
-- VACUUM. VACUUM is safe (it rebuilds content without deleting rows, so
-- Phase 0's incremental_vacuum is unaffected). DROP TABLE and DROP TRIGGER
-- fire nothing and cannot be refused in SQL — a connection that drops the
-- guard can then delete freely. The gate answers that one at runtime instead:
-- it verifies both triggers are present in sqlite_master before it will run,
-- and refuses with `guard_missing` if either has been removed.
--
-- No existing code path is broken by this. Scanned 1665 .py/.sql files: the
-- only DELETE against agent_events or agent_sessions anywhere in the tree is
-- fixture teardown in tests/sense/test_extract_then_null.py. The compactor
-- UPDATEs (lifecycle.py:316) and the ingester upserts via ON CONFLICT(uuid)
-- DO UPDATE (trace/claude_code.py:290) — neither is a delete, both pinned by
-- tests added with this migration.

-- ---------------------------------------------------------------------------
-- 1. Configuration — the ratification switch
-- ---------------------------------------------------------------------------
-- One row, forced by the CHECK. Two fields that must not be conflated:
--
-- deletion_enabled is THE OWNER'S SWITCH, not the pipeline's. It ships 0 and
-- nothing in okuro may write it — no daemon, no CLI, no gate. Phase 1 through 3
-- run to completion with it off, and every real gate invocation refuses. That
-- refusal is the correct behaviour today, not a bug to be worked around.
--
-- min_deletable_rubric_version is a FLOOR that is maintained separately from
-- the current rubric version, and it is deliberately NOT ">= current". Gating
-- on "distilled with the newest rubric" means the first rubric bump instantly
-- disqualifies the entire distilled corpus, and deletion silently stops
-- forever with nothing in the output to say why. The floor moves only when
-- somebody decides an older rubric is no longer trustworthy.
CREATE TABLE IF NOT EXISTS distill_config (
    id                            INTEGER PRIMARY KEY CHECK (id = 1),
    min_deletable_rubric_version  INTEGER NOT NULL DEFAULT 1,
    deletion_enabled              INTEGER NOT NULL DEFAULT 0
                                  CHECK (deletion_enabled IN (0, 1)),
    updated_at                    TEXT DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO distill_config (id) VALUES (1);

-- ---------------------------------------------------------------------------
-- 2. The qualification ledger
-- ---------------------------------------------------------------------------
-- One row per distilled session. This row IS the precondition the standing
-- constraint names: its existence, at a trusted rubric version, is what makes
-- the raw transcript redundant.
--
-- No foreign key to agent_sessions. The ledger has to outlive whatever happens
-- to the session it describes, and a FK here would invite exactly the cascade
-- this migration spends a trigger preventing.
--
-- Dimension scores are REAL in [0,1] rather than a point scale. Rubrics change
-- and point scales do not survive the change; a normalised score plus the
-- rubric_version that produced it does. NULL means the judge could not assess
-- that dimension — distinct from 0, which means it assessed and found nothing.
--
-- evidence_quotes is length-CHECKed, not merely documented as bounded. The
-- table exists so raw text can be deleted; an unbounded quote field would
-- quietly become a second copy of the transcript and defeat the whole exercise.
CREATE TABLE IF NOT EXISTS session_distillations (
    session_id            TEXT PRIMARY KEY,   -- agent_sessions.session_id (native)
    rubric_version        INTEGER NOT NULL,
    distilled_at          TEXT NOT NULL DEFAULT (datetime('now')),
    verdict               TEXT NOT NULL
                          CHECK (verdict IN ('good', 'bad', 'mixed', 'unusable')),

    -- Dimension scores, 0.0-1.0, NULL = not assessed.
    score_protocol_compliance     REAL CHECK (score_protocol_compliance     IS NULL OR (score_protocol_compliance     BETWEEN 0.0 AND 1.0)),
    score_task_outcome            REAL CHECK (score_task_outcome            IS NULL OR (score_task_outcome            BETWEEN 0.0 AND 1.0)),
    score_communication_contract  REAL CHECK (score_communication_contract  IS NULL OR (score_communication_contract  BETWEEN 0.0 AND 1.0)),
    score_tool_routing            REAL CHECK (score_tool_routing            IS NULL OR (score_tool_routing            BETWEEN 0.0 AND 1.0)),

    -- JSON array of short quotes. Bounded by CHECK, see above.
    evidence_quotes       TEXT NOT NULL DEFAULT '[]'
                          CHECK (LENGTH(evidence_quotes) <= 4096),
    -- JSON array of distill_lessons.id
    lesson_refs           TEXT NOT NULL DEFAULT '[]',

    judge_model           TEXT,
    judge_tokens          INTEGER,
    -- NULL only for tier-0/tier-1 rows, which are not validation-sampled.
    validation_batch_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_distillations_rubric
    ON session_distillations(rubric_version);
CREATE INDEX IF NOT EXISTS idx_session_distillations_verdict
    ON session_distillations(verdict);

-- ---------------------------------------------------------------------------
-- 3. Lessons (ACE-style playbook entries)
-- ---------------------------------------------------------------------------
-- Distillation produces two things: a per-session verdict (above) and reusable
-- lessons (here). helpful_count / harmful_count are the ACE feedback pair —
-- a lesson earns its place by being cited usefully, and earns retirement by
-- being cited harmfully.
--
-- `lesson_class` rather than `class`: legal in SQLite, but every Python caller
-- would have to spell it `row["class"]` next to an actual class statement.
--
-- embedding_id is a REFERENCE, deliberately unpopulated in Phase 1. Creating a
-- vec0 table now would cost 4 MiB per chunk-block per partition value for zero
-- Phase 1 benefit, and lessons are not session-scoped so the partition-key
-- question does not even arise yet. Phase 2 adds the table (session-level,
-- unpartitioned) and backfills through embed/repair.py::partition_value().
CREATE TABLE IF NOT EXISTS distill_lessons (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_text           TEXT NOT NULL,
    lesson_class          TEXT NOT NULL
                          CHECK (lesson_class IN ('instruction-defect',
                                                  'role-pitfall',
                                                  'protocol-gap',
                                                  'harness-bug')),
    helpful_count         INTEGER NOT NULL DEFAULT 0,
    harmful_count         INTEGER NOT NULL DEFAULT 0,
    embedding_id          TEXT,               -- Phase 2; NULL throughout Phase 1
    status                TEXT NOT NULL DEFAULT 'candidate'
                          CHECK (status IN ('candidate', 'active', 'retired')),
    -- JSON array of session_ids that evidence this lesson.
    evidence_session_ids  TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_distill_lessons_status
    ON distill_lessons(status, lesson_class);

-- ---------------------------------------------------------------------------
-- 4. Tombstones — what was deleted, and where the only remaining copy lives
-- ---------------------------------------------------------------------------
-- PRIMARY KEY on session_id is the "written exactly once" guarantee: a second
-- gate pass over the same session cannot silently produce a second tombstone
-- with a different archive path.
--
-- archive_path and archive_sha256 are not bookkeeping. After the gate runs,
-- the archive is the ONLY copy of that session's events — the DB was measured
-- as a 3.9x superset of what remains on disk. A tombstone without a verified
-- archive is a record of unrecoverable loss.
CREATE TABLE IF NOT EXISTS distill_tombstones (
    session_id      TEXT PRIMARY KEY,
    deleted_at      TEXT NOT NULL DEFAULT (datetime('now')),
    events_deleted  INTEGER NOT NULL,
    archive_path    TEXT NOT NULL,
    archive_sha256  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 5. The guard
-- ---------------------------------------------------------------------------
-- Fires BEFORE DELETE so nothing is removed and no AFTER trigger (including
-- the FTS _ad sync from 016) observes a half-applied delete. On the armed
-- path this trigger is a no-op and the FTS side proceeds exactly as before.
CREATE TRIGGER IF NOT EXISTS agent_events_retention_guard
BEFORE DELETE ON agent_events
WHEN okuro_distill_gate_armed() IS NOT 1
BEGIN
    -- One literal, deliberately. SQL has no adjacent-string concatenation:
    -- splitting this across two quoted lines is a syntax error, not a longer
    -- message.
    SELECT RAISE(ABORT, 'agent_events is delete-protected — route deletes through okuro.sense.distill.gate.delete_session_events()');
END;

-- ---------------------------------------------------------------------------
-- 6. The REPLACE guard
-- ---------------------------------------------------------------------------
-- Refuses an INSERT onto an EXISTING uuid unless the connection has armed
-- `okuro_trace_upsert_armed()`. That is the whole discrimination: a REPLACE
-- and an upsert look identical here, so the only thing separating "the
-- ingester meant to rewrite this event" from "something is about to destroy
-- it" is whether the caller said so.
--
-- Inserting a NEW uuid is untouched, which is the overwhelmingly common path
-- and stays a single indexed PK probe.
--
-- The function is registered DISARMED on every okuro connection
-- (db/sqlite.py::_make_conn) and armed only by okuro.trace.trace_upsert_armed()
-- around the four ingesters' upserts. A raw sqlite3 shell cannot define it at
-- all, so nothing it issues against agent_events compiles — new uuid or not.
--
-- Reads are entirely unaffected: this is an INSERT trigger.
CREATE TRIGGER IF NOT EXISTS agent_events_replace_guard
BEFORE INSERT ON agent_events
WHEN okuro_trace_upsert_armed() IS NOT 1
 AND EXISTS (SELECT 1 FROM agent_events WHERE uuid = NEW.uuid)
BEGIN
    SELECT RAISE(ABORT, 'agent_events: refusing to overwrite an existing event — INSERT OR REPLACE deletes the row it replaces. Use ON CONFLICT(uuid) DO UPDATE inside okuro.trace.trace_upsert_armed()');
END;
