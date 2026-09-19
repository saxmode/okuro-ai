-- <!-- AGENT_HEADER
-- role: code
-- purpose: The checks table — where a maintenance sweep's "I looked and
--   nothing moved" goes, now that it is no longer allowed to pretend to be
--   knowledge. Plus the one-time move of the 386 rows that already did.
-- index: content
-- AGENT_HEADER_END -->
--
-- THE CLASS THIS FIXES: AN OUTCOME FILED UNDER THE WRONG NOUN.
--
-- A daily sweep asks 104 roles "did anything change". For most roles, on most
-- days, the honest answer is no. That answer is a real, valuable observation:
-- it says the role was checked, by whom, against what, and when. It is not
-- knowledge. Nothing was learned.
--
-- okuro filed it as knowledge anyway, because `roles_learn` was the only verb
-- the sweep had. Measured on the live store 2026-09-17, read-only:
--
--     role_knowledge rows                                     844
--     rows containing "no significant change" (case-insens.)  386
--     roles carrying at least one such row                     94 of 104
--     monthly counts Apr→Sep 2026            2 / 24 / 47 / 109 / 113 / 91
--
-- Forty-six percent of the store. And the trend is the wrong way up: the
-- sweep's filler output QUADRUPLED between May and August while concrete
-- findings fell. Every one of those rows was indexed into `vec_knowledge`,
-- which means every semantic search a dispatched subagent runs against a
-- role's knowledge competes against 386 rows that say nothing, and the
-- retrieval pool is finite. A store that grows by 90 rows a month without
-- gaining a fact is not a knowledge store, it is a log with the wrong name.
--
-- So the noun changes. `role_knowledge_checks` holds the observation; the
-- outcome column names what was seen; `role_knowledge` goes back to holding
-- only rows that carry a fact.
--
-- ------------------------------------------------------------------------
-- WHY THE MARKER MATCH IS SAFE AS A ONE-TIME MOVE AND WRONG AS A LIVING RULE.
--
-- This migration identifies the rows by the substring the sweep's prompt told
-- it to write. That is a text heuristic, and a text heuristic is exactly the
-- kind of control this whole phase exists to replace — it holds only as long
-- as the wording does, and the wording lives in a YAML file nobody versioned.
--
-- It is used here because it is the ONLY evidence the existing 386 rows carry,
-- and a one-time backfill against a frozen corpus can be verified by reading
-- it. It is NOT the going-forward mechanism: `roles.knowledge.write_knowledge`
-- now routes a no-change entry at the CODE level, and `roles_learn` grew an
-- explicit `outcome` parameter so a caller can say so without the string. The
-- marker stays in the code as a fallback for an agent that only writes prose,
-- never as the primary signal.
--
-- ------------------------------------------------------------------------
-- WHY `roles.learnings` IS NOT TOUCHED, EVEN THOUGH IT COUNTED THESE ROWS.
--
-- It did count them, and the measurement is worth recording because it is the
-- reason this paragraph is not a shrug:
--
--   * `roles.learnings` has exactly ONE incrementing writer in the tree —
--     `roles/knowledge.py:write_knowledge`, which bumps it unconditionally on
--     every insert.
--   * All 386 rows are `type='research'`. 360 carry a `session_id`, which only
--     that path sets.
--   * The other 26 are not `roles/drafter.py` seeds either: every one was
--     created 68-70 days AFTER its role's `created_at`, and zero of the 386
--     share a creation DATE with their role's birth.
--
-- So the counter is now overstating by exactly the number of rows moved, and
-- this migration still may not fix it. `learnings` is in
-- `roles/columns.py:RUNTIME_OWNED_COLUMNS`, and that split is enforced by
-- `tests/roles/test_no_role_catalog.py`, not merely described. The rule reads:
-- a migration that writes one of these is overwriting something the database
-- earned, and there is no version of that which is correct.
--
-- The rule is right here, and the evidence for it is in the same measurement.
-- The counter ALREADY disagrees with the row count for 64 of the 101 roles
-- that have knowledge — `drafter.py` inserts rows without bumping it, so it
-- was never a mirror of the table. A migration subtracting from a number it
-- cannot reconcile is guessing with authority.
--
-- WHAT THIS LEAVES, stated plainly rather than left to be discovered:
--   * `SUM(learnings)` reads 760 against 458 remaining knowledge rows. The
--     overcount is per-role equal to the rows moved out of that role.
--   * Four roles sit at `learnings >= 20`, which is half the auto-maturity
--     gate (`sessions >= 10 AND learnings >= 20` in `_check_auto_mature`).
--     Nothing is demoted — that function only promotes and the store has 0
--     mature roles — but those four could mature on sweep receipts.
--   * Nothing else reads it. In particular the fit scorer does not: the
--     knowledge segment counts ROWS, which this migration makes correct.
--
-- The repair belongs to a runtime writer, not to this file. Reconciling it is
-- a data repair against a counter three code paths touch, which is the job the
-- structural update pipeline exists for.
--
-- ------------------------------------------------------------------------
-- WHAT IS DELETED AND WHAT CANNOT BREAK.
--
-- Verified against the live schema, not assumed:
--   * `role_knowledge` has NO triggers. Nothing fires on the delete.
--   * Its only inbound FK is its own `supersedes` self-reference. ZERO
--     surviving rows point at a row being moved, and ZERO moved rows point at
--     anything, so the delete breaks no chain.
--   * `vec_knowledge` is `vec0(id TEXT PRIMARY KEY, role_id partition key,
--     embedding float[1024])`. Its `id` IS the `role_knowledge.id`, so the
--     vector rows are addressable by the same key and are deleted here. They
--     are NOT cascaded by anything — a vec0 virtual table has no foreign keys,
--     which is precisely why this has to be explicit. Leaving them would keep
--     386 dead vectors ranking in every scoped knowledge search, pointing at
--     rows whose lookup now returns nothing.
--
-- The moved row KEEPS ITS ID as the checks-row primary key, and `note` names
-- where it came from in words. Two forms of the same traceability on purpose:
-- the id makes the move reversible by join, the note makes it readable by a
-- human who found the row three months from now and has no migration in hand.
--
-- IDEMPOTENT. `INSERT OR IGNORE` on the carried-over primary key, and every
-- other statement is driven by a SELECT over rows this migration deletes — so
-- a second apply moves nothing and deletes nothing.
--
-- ------------------------------------------------------------------------
-- ROLLBACK (statements, not a file — this repo has no rollback files).
--
-- The check rows carry everything needed to rebuild the knowledge rows EXCEPT
-- `content`, which is the whole point: the prose said nothing. Restoring the
-- text means the pre-migrate snapshot the runner takes automatically
-- (db/sqlite.py:_snapshot_before_migrate, written next to okuro.db). Restore
-- the ROWS, not the file — a file-level restore also rewinds every other table
-- that migrate run touched, including the sessions/learnings counters, which
-- tick continuously:
--
--     ATTACH '<snapshot path>' AS pre;
--     INSERT INTO role_knowledge
--       SELECT * FROM pre.role_knowledge
--        WHERE id IN (SELECT id FROM role_knowledge_checks
--                      WHERE note LIKE 'migrated by 159 %');
--     DETACH pre;
--     DROP TABLE role_knowledge_checks;
--     DELETE FROM _migrations WHERE name = '159_role_knowledge_checks.sql';
--
-- The vectors are NOT restored by that. Re-embed them with
-- `python -m okuro.embed.repair`, which rebuilds `vec_knowledge` from
-- `role_knowledge` — the reason the restore order above puts the rows back
-- before anything looks at the index.


CREATE TABLE IF NOT EXISTS role_knowledge_checks (
    id         TEXT PRIMARY KEY,
    role_id    TEXT NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    checked_at TEXT DEFAULT (datetime('now')),
    -- WHAT the sweep saw. Three outcomes, and the two that are not
    -- `no_change` are the reason this is a column rather than a boolean:
    --   no_change — looked, nothing material moved. The common case.
    --   error     — tried to look and could not. NOT the same as quiet, and
    --               the old filler row could not tell those apart.
    --   skipped   — did not look. Out of scope, rate-limited, budget spent.
    -- A sweep that reports `error` for a role for six weeks is a dead feed;
    -- as a "no significant change" knowledge row it was indistinguishable
    -- from six weeks of a stable source.
    outcome    TEXT NOT NULL
               CHECK (outcome IN ('no_change', 'error', 'skipped')),
    -- The poll/sweep run this observation belongs to, when the caller has one.
    -- NULL for the migrated rows: they predate run ids entirely.
    run_id     TEXT,
    -- What was checked, when the checker named it. A claim, not proof — the
    -- proof lives in `source_fetches` and is reached through `run_id`.
    source_url TEXT,
    -- WHO observed this. Carried for the same reason `role_knowledge` carries
    -- it, and it is not redundant with `run_id`: 360 of the 386 rows this
    -- migration moves have a session_id and NONE has a run_id, because they
    -- predate run ids entirely. Without this column the move would make every
    -- one of them unattributable — the audit trail would end at "some sweep,
    -- some time in July".
    session_id TEXT,
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_role_knowledge_checks_role
    ON role_knowledge_checks(role_id);
-- The recency read: "when was this role last checked at all" is the query the
-- fit scorer runs per role, and it orders by this column.
CREATE INDEX IF NOT EXISTS idx_role_knowledge_checks_checked
    ON role_knowledge_checks(checked_at);
CREATE INDEX IF NOT EXISTS idx_role_knowledge_checks_run
    ON role_knowledge_checks(run_id);
CREATE INDEX IF NOT EXISTS idx_role_knowledge_checks_session
    ON role_knowledge_checks(session_id);


-- ── The move, in the only order that works ───────────────────────────
--
-- Insert, then delete. The insert READS `role_knowledge`, so the delete has to
-- come last; the other order moves nothing and reports success.

INSERT OR IGNORE INTO role_knowledge_checks
    (id, role_id, checked_at, outcome, run_id, source_url, session_id, note)
SELECT
    k.id,
    k.role_id,
    k.created_at,
    'no_change',
    NULL,
    k.source_url,
    k.session_id,
    'migrated by 159 from role_knowledge.id=' || k.id
FROM role_knowledge k
WHERE lower(k.content) LIKE '%no significant change%';


-- ── The vectors ──────────────────────────────────────────────────────
--
-- WHY THIS MIGRATION CREATES A vec0 TABLE, WHICH NO OTHER ONE DOES.
--
-- The convention in this tree is that migrations never touch vec0 tables —
-- migration 059 says so outright, and `vec_knowledge` is created at runtime by
-- `embed.repair.ensure_vec_dims`, which the migration runner calls immediately
-- after this pass. The convention exists because the embedding DIMENSION is a
-- runtime property of the active tier and a migration cannot read it.
--
-- On a FRESH install that means `vec_knowledge` does not exist yet when this
-- file runs, and a bare DELETE against it aborts the whole migration. On an
-- EXISTING install it does exist and holds 386 vectors that must go: they are
-- not cascaded by anything (a vec0 table has no foreign keys), and each one
-- occupies a slot in a finite KNN candidate pool while pointing at a row whose
-- lookup now returns nothing. Leaving them is the retrieval half of the defect
-- this migration exists to fix.
--
-- So the table is created only IF ABSENT, which by construction is only the
-- fresh-install case — where it is created EMPTY, `role_knowledge` is empty
-- too, and `ensure_vec_dims(empty_only=True)` rebuilds any empty table whose
-- declared dim does not match the active tier seconds later. The shape below
-- is the one `embed.repair._create_sql` writes for the current default tier,
-- so on the common install it is already right and the rebuild is a no-op.
-- Verified by applying the full migration set to a fresh store and reading the
-- resulting DDL back.
CREATE VIRTUAL TABLE IF NOT EXISTS vec_knowledge USING vec0(
    id TEXT PRIMARY KEY,
    role_id TEXT partition key,
    embedding float[1024] distance_metric=cosine
);

-- Vectors first, rows second. The other order leaves the vec delete with no
-- subquery to drive it, and a vec0 table has no cascade to fall back on.
DELETE FROM vec_knowledge
 WHERE id IN (
           SELECT id FROM role_knowledge
            WHERE lower(content) LIKE '%no significant change%'
       );

DELETE FROM role_knowledge
 WHERE lower(content) LIKE '%no significant change%';
