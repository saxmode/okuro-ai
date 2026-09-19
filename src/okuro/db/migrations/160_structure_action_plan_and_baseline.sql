-- <!-- AGENT_HEADER
-- role: code
-- purpose: What the update pipeline has to remember about its own write — the
--   computed plan, the baseline it will be judged against, the columns it
--   touched, where the generated migration was left, and the state a rollback
--   puts the row in.
-- index: content
-- AGENT_HEADER_END -->
--
-- THE CLASS THIS FIXES: A GATE WHOSE INPUT THE CALLER SUPPLIES.
--
-- Migration 158 gave `→ verified` a real comparison: same rubric_version on
-- both sides, structure score no lower after than before. Its own report named
-- the hole in it, in the docstring of the function that performs the check —
-- "the baseline is caller-supplied, and that is a real limit worth stating
-- rather than hiding: nothing in this table stores the pre-change score".
--
-- A gate whose reference point arrives in the same call that asks to pass it
-- is not a gate. The caller that wants to be verified picks the number it is
-- compared against, and picking a low one is not even dishonest — a plausible
-- `fit_before` is exactly what an agent re-deriving the baseline AFTER the
-- write would produce, because the bodies it would score are the new ones.
--
-- So the baseline is taken at PLAN time, by the pipeline, before anything is
-- written, and stored here. `verify` reads this column and ignores anything a
-- caller offers. The reading is a list of whole `compute_fit` results, one per
-- affected role, each carrying its own `rubric_version` — so A5's refusal
-- ("a score taken under a different rubric is a different measurement") stays
-- enforceable against the stored side too, not only against the fresh one.
--
-- ------------------------------------------------------------------------
-- WHY THE PLAN IS STORED AND NOT RECOMPUTED.
--
-- The dry-run diff is what the owner approves. If `implement` recomputed the
-- plan from the operations, it would approve one document and write another
-- the moment any part of the computation is not deterministic — and one part
-- is not: sections authored through the bridge. Two calls, two wordings, and
-- the diff that was reviewed describes text that no longer exists.
--
-- Storing the computed plan — the operations, the BEFORE bodies and the
-- resulting ones — makes "what was approved is what lands" a property of the
-- row rather than a promise. `dry_run` renders this column; `implement`
-- writes it; `verify` scores what it wrote; `restore` needs the before text
-- for the columns a snapshot row cannot distinguish from an unrelated edit.
--
-- TEXT, holding JSON, like every other structured column in this store. It is
-- large (six bodies per affected role) and that is the honest size of the
-- thing: a plan that fits in a summary is a plan nobody can check. It is also
-- why `list_actions` and every transition return exclude it — see
-- roles/actions.py:HEAVY_COLUMNS.
--
-- ------------------------------------------------------------------------
-- WHY THE TABLE IS REBUILT AND NOT EXTENDED.
--
-- `restored` is a new state, and a state lives in a CHECK constraint that
-- SQLite cannot ALTER. Rebuild is the documented procedure and migration 063
-- is the house precedent for it, PRAGMA and all.
--
-- The state exists because a rollback used to be invisible. `restore` put the
-- bodies back and left the row reading `implemented` or `verified` — so the
-- panel, the todo and every later reader saw a change that was in force when
-- it had been taken out, and the only trace was one line in the event log.
-- A row whose write has been reversed is not in the same state as a row whose
-- write stands.
--
-- The three other new columns are what a reversal needs and the row did not
-- carry:
--
--   * `written_columns` — the exact column set `implement` wrote, per role.
--     Restoring "the three bodies and the tier" was a guess that was wrong
--     the moment an operation touched `model` or `maintenance_schedule`, and
--     wrong silently: those columns kept the new value while the bodies went
--     back, which is a row in neither state.
--
--   * `migration_outbox_path` — where the generated migration was LEFT. It is
--     not written into any checkout any more (see roles/structural_update.py
--     on the outbox), so the row is the only thing that knows where it went.
--
--   * `evidence_artifact_id` — the pre-change bodies. It used to be written
--     into `diff_artifact_id`, which OVERWROTE the dry-run diff that was
--     approved. The one document proving what the decision was about was
--     replaced by a document produced after it.

PRAGMA foreign_keys=OFF;

CREATE TABLE role_structure_actions_new (
    id                      TEXT PRIMARY KEY,
    kind                    TEXT NOT NULL
                            CHECK (kind IN ('finding', 'source_health')),
    -- `restored` joins the terminal set. It is reachable only from
    -- `implemented` and `verified`: there is nothing to reverse before a
    -- write, and a row that never wrote must not be able to claim it did.
    state                   TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (state IN (
                                'proposed', 'researched', 'planned',
                                'critiqued', 'approved', 'implemented',
                                'verified', 'restored', 'rejected',
                                'superseded')),
    title                   TEXT NOT NULL,

    source_id               TEXT
                            REFERENCES role_structure_sources(id)
                            ON DELETE SET NULL,
    run_id                  TEXT,
    fetch_id                TEXT
                            REFERENCES source_fetches(id) ON DELETE SET NULL,
    evidence_url            TEXT,
    quoted_sentence         TEXT,
    quote_verified          INTEGER DEFAULT 0,

    okuro_element           TEXT,
    affected_role_ids       TEXT DEFAULT '[]',

    rubric_version          TEXT,
    source_hash_at_proposal TEXT,

    research_artifact_id    TEXT,
    plan_artifact_id        TEXT,
    critique_artifact_id    TEXT,
    -- IMMUTABLE ONCE THE ROW IS APPROVED. This is the document the decision
    -- was about; a later write to it re-labels what was decided.
    diff_artifact_id        TEXT,
    -- The pre-change bodies, written by `implement`. Its own column so it
    -- cannot land on top of the diff.
    evidence_artifact_id    TEXT,
    migration_id            TEXT,
    migration_outbox_path   TEXT,

    todo_id                 TEXT,
    last_refusal            TEXT,

    -- The computed plan (operations + before and after bodies) and the fit
    -- reading taken before the write. Both are large; both are excluded from
    -- list and transition responses.
    plan_json               TEXT,
    fit_baseline            TEXT,
    -- JSON {role_id: [column, ...]} — what `implement` actually wrote.
    written_columns         TEXT,

    created_by              TEXT,
    created_at              TEXT DEFAULT (datetime('now')),
    decided_by              TEXT,
    decided_at              TEXT,
    superseded_by           TEXT
                            REFERENCES role_structure_actions(id)
                            ON DELETE SET NULL,
    updated_at              TEXT DEFAULT (datetime('now'))
);

INSERT INTO role_structure_actions_new (
    id, kind, state, title, source_id, run_id, fetch_id, evidence_url,
    quoted_sentence, quote_verified, okuro_element, affected_role_ids,
    rubric_version, source_hash_at_proposal, research_artifact_id,
    plan_artifact_id, critique_artifact_id, diff_artifact_id, migration_id,
    todo_id, last_refusal, created_by, created_at, decided_by, decided_at,
    superseded_by, updated_at
)
SELECT
    id, kind, state, title, source_id, run_id, fetch_id, evidence_url,
    quoted_sentence, quote_verified, okuro_element, affected_role_ids,
    rubric_version, source_hash_at_proposal, research_artifact_id,
    plan_artifact_id, critique_artifact_id, diff_artifact_id, migration_id,
    todo_id, last_refusal, created_by, created_at, decided_by, decided_at,
    superseded_by, updated_at
FROM role_structure_actions;

DROP TABLE role_structure_actions;
ALTER TABLE role_structure_actions_new RENAME TO role_structure_actions;

-- Every index 158 created, recreated against the new table. They do not
-- survive the rebuild and a missing index here is a slow panel nobody
-- connects to this migration six months from now.
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_state
    ON role_structure_actions(state);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_kind
    ON role_structure_actions(kind, state);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_source
    ON role_structure_actions(source_id);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_run
    ON role_structure_actions(run_id);

-- The source_health dedupe. `restored` joins the excluded set for the same
-- reason the other three are in it: an alarm whose row has been dealt with
-- must be able to open a new row rather than silently reuse the closed one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_structure_actions_open_health
    ON role_structure_actions(source_id, title)
    WHERE kind = 'source_health'
      AND state NOT IN ('verified', 'restored', 'rejected', 'superseded');

PRAGMA foreign_keys=ON;
