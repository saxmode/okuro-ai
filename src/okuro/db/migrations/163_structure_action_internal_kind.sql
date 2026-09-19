-- <!-- AGENT_HEADER
-- role: code
-- purpose: A third kind of structural action — one whose evidence is okuro's
--   own measurement of its own store, because there is no vendor page to
--   quote when the finding is about okuro.
-- index: content
-- AGENT_HEADER_END -->
--
-- THE GAP THIS CLOSES.
--
-- `roles/actions.propose` has two kinds and neither fits a finding okuro made
-- about itself:
--
--   * `finding` demands a verified quote — `verify_quote(run_id, source_id,
--     sentence)` against the body a poll run actually fetched. That gate is
--     right and it stays: three role-refresh fabrications each passed their
--     own prose acceptance criteria, and a substring assert against a stored
--     body is the thing that does not depend on anybody's good faith.
--
--   * `source_health` is the poller's own alarm about a feed.
--
-- An INTERNAL finding has no vendor source and no sentence to quote. "The
-- structure gate stopped counting markers inside a fenced code block, and
-- system-documenter has no real KNOWLEDGE PROTOCOL section" is not on any
-- registered page — it is a measurement over the `roles` table. Before this
-- migration there was NO WAY TO OPEN AN ACTION FOR IT, so okuro could audit
-- itself and then had nowhere to put the result except prose.
--
-- ------------------------------------------------------------------------
-- WHAT REPLACES THE QUOTE, AND WHY IT IS NOT WEAKER.
--
-- The quote gate answers one question: can this claim be checked against
-- something the caller does not control? For an internal finding the answer
-- is better than a quote, because the subject is in this database:
--
--   1. `finding_artifact_id` must name an artifact that EXISTS — the
--      measurement, with its method, so the reading can be reproduced.
--   2. `okuro_element` must name a section label or micro key, validated
--      against the live `designer` constants exactly as `finding` validates
--      it, and narrowed further: an element a role cannot be MEASURED
--      against (a designer constant, a `roles` column) is refused, because
--      the next gate would have nothing to recompute.
--   3. `affected_role_ids` must be explicit, and EVERY named role is
--      re-measured at propose time. A role that currently CARRIES the named
--      element refuses the whole proposal. An internal finding that cannot
--      be reproduced against the store does not exist — the same sentence
--      the quote gate enforces, with the store in place of the fetched body.
--
-- So the reference point is recomputed, never supplied. `rubric_version` is
-- stamped by the code for the same reason it is for `finding`: a caller that
-- brings its own reference point has brought the thing the reference point
-- exists to fix.
--
-- ------------------------------------------------------------------------
-- WHY A NEW COLUMN AND NOT `evidence_artifact_id`.
--
-- `evidence_artifact_id` is already taken, by the write path: migration 160
-- gave it to `implement`, which stores the PRE-CHANGE BODIES there. It got
-- its own column in 160 precisely so it would stop landing on top of
-- `diff_artifact_id` and re-labelling what the approval was about.
--
-- Putting an internal finding's proof in that column would reintroduce that
-- defect one column over: propose writes the measurement, the owner approves,
-- implement overwrites it with the pre-change bodies, and the one document
-- showing the finding was real is gone — replaced, again, by a document
-- produced after the decision.
--
-- Hence `finding_artifact_id`: written once, at propose, by the proposer.
-- The two names are close and the distinction is not cosmetic —
-- `finding_artifact_id` is why the row was opened, `evidence_artifact_id` is
-- what the write took out.
--
-- ------------------------------------------------------------------------
-- WHY THE TABLE IS REBUILT.
--
-- `kind` lives in a CHECK constraint and SQLite cannot ALTER one. Rebuild is
-- the documented procedure; migration 160 is the immediate precedent and 063
-- the house one, PRAGMA and all. Every index 158 created and 160 recreated is
-- recreated again below — they do not survive the rebuild, and a missing
-- index here is a slow panel nobody connects to this migration later.

PRAGMA foreign_keys=OFF;

CREATE TABLE role_structure_actions_new (
    id                      TEXT PRIMARY KEY,
    -- `internal` joins the two kinds 158 shipped. Its gate is in
    -- roles/actions.py:propose — no source, no quote, a reproducible
    -- measurement instead.
    kind                    TEXT NOT NULL
                            CHECK (kind IN ('finding', 'source_health',
                                            'internal')),
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
    -- WHY the row was opened: the measurement behind an internal finding,
    -- written once at propose. Read the header above on why this is not
    -- `evidence_artifact_id`.
    finding_artifact_id     TEXT,
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
    plan_artifact_id, critique_artifact_id, diff_artifact_id,
    evidence_artifact_id, migration_id, migration_outbox_path,
    todo_id, last_refusal, plan_json, fit_baseline, written_columns,
    created_by, created_at, decided_by, decided_at, superseded_by, updated_at
)
SELECT
    id, kind, state, title, source_id, run_id, fetch_id, evidence_url,
    quoted_sentence, quote_verified, okuro_element, affected_role_ids,
    rubric_version, source_hash_at_proposal, research_artifact_id,
    plan_artifact_id, critique_artifact_id, diff_artifact_id,
    evidence_artifact_id, migration_id, migration_outbox_path,
    todo_id, last_refusal, plan_json, fit_baseline, written_columns,
    created_by, created_at, decided_by, decided_at, superseded_by, updated_at
FROM role_structure_actions;

DROP TABLE role_structure_actions;
ALTER TABLE role_structure_actions_new RENAME TO role_structure_actions;

CREATE INDEX IF NOT EXISTS idx_role_structure_actions_state
    ON role_structure_actions(state);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_kind
    ON role_structure_actions(kind, state);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_source
    ON role_structure_actions(source_id);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_run
    ON role_structure_actions(run_id);

-- The source_health dedupe, unchanged. `internal` is deliberately NOT
-- deduplicated this way: its identity is (element, affected roles), not
-- (source, title), and a source_id it does not have cannot key an index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_role_structure_actions_open_health
    ON role_structure_actions(source_id, title)
    WHERE kind = 'source_health'
      AND state NOT IN ('verified', 'restored', 'rejected', 'superseded');

PRAGMA foreign_keys=ON;
