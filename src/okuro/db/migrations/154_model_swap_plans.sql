-- <!-- AGENT_HEADER
-- role: code
-- purpose: 154_model_swap_plans — P5 of okuro-model-manager. The swap plan:
--   replace model X in consumer Y with Z, as a set of PROPOSED line edits that
--   nothing executes. One row per config line the swap would touch, tied by
--   `plan_group`, carrying the rewritten reference in the SAME SHAPE the line
--   already uses. A PROTECTED consumer cannot reach `applied` until every
--   checklist item is ticked (ruling 7), and a headroom shortfall WARNS and
--   never refuses (ruling 8).
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive. Nothing here is populated by the migration: every row is written
-- by `okuro.ai_models.swap.propose`.
--
-- WHAT THIS TABLE IS NOT. It is not an edit queue and there is no executor.
-- Plan v1 §4 is explicit: the manager "does not auto-edit consumer configs. It
-- shows the exact file:line and the diff." `applied` therefore records that the
-- HUMAN applied it, and `swap.apply` prints the diff to apply by hand. A column
-- named `state` that reaches `applied` without a writer anywhere in this repo
-- is the safety property, not an unfinished feature.
--
-- WHY ONE ROW PER LINE AND NOT ONE PER SWAP. A single swap on this host's
-- workflow tree touches thirteen lines across seven files, in four different
-- reference SHAPES (an absolute path, a bucket-relative suffix, a bare
-- filename, a registry alias). The unit of review is the LINE, because that is
-- what a person has to open and change, and because each line's rewrite can
-- fail on its own — a reference naming a shared parent DIRECTORY cannot be
-- rewritten at all without repointing every model underneath it.
--
-- THE COST OF THAT CHOICE, NAMED RATHER THAN HIDDEN. The group-level facts
-- (state, checklist, blockers, mode, tier, relation, unit_old, new_ref) are
-- repeated on every row of a group, so two rows of one plan could in principle
-- disagree. Every mutation in `swap.py` therefore writes by `plan_group` and
-- never by `id`, and a test asserts the group stays uniform after a tick, an
-- apply and a reject. If you would rather pay a join than trust that
-- discipline, this splits into a header table and an edit table with no change
-- to the module's public surface.

CREATE TABLE IF NOT EXISTS model_swap_plans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_group       TEXT NOT NULL,            -- the id a person addresses: sw-xxxxxxxx

    -- --- what is being swapped, for whom -----------------------------------
    consumer         TEXT NOT NULL,
    tier             TEXT NOT NULL,            -- PROTECTED | ACTIVE | ARCHIVE, COPIED
                                               -- from the consumer at propose time.
                                               -- Copied on purpose: a plan is judged
                                               -- against the ruling that held when it
                                               -- was proposed, and a config edit must
                                               -- not silently downgrade an open plan's
                                               -- gate.
    unit_old         TEXT NOT NULL,            -- model_units.unit_id being replaced
    new_kind         TEXT NOT NULL,            -- unit | release
    new_ref          TEXT NOT NULL,            -- unit_id, or the HuggingFace id
    relation         TEXT,                     -- P3's lineage verdict, old -> new.
                                               -- NULL = not asked (neither name parsed
                                               -- to a family), which is a different
                                               -- fact from 'unrelated'. The rule 152
                                               -- set for model_discoveries.relation.
    relation_why     TEXT,
    mode             TEXT NOT NULL,            -- replace | side-by-side

    -- --- the one line this row is about ------------------------------------
    config_path      TEXT NOT NULL,
    line             INTEGER NOT NULL,
    old_ref          TEXT NOT NULL,            -- exactly as the line writes it
    new_ref_written  TEXT,                     -- the rewrite, in the SAME shape:
                                               -- absolute stays absolute, a container
                                               -- path stays a container path, a
                                               -- bucket-relative suffix keeps its
                                               -- segment count, an alias stays an
                                               -- alias. NULL = this line cannot be
                                               -- rewritten mechanically; `rewrite_note`
                                               -- says why.
    match_kind       TEXT,                     -- how P2 resolved the old reference.
                                               -- Carried because it DECIDES the rewrite
                                               -- shape, and because a bare-filename
                                               -- match is weaker evidence than an
                                               -- absolute-path match.
    rewritable       INTEGER NOT NULL DEFAULT 1,
    rewrite_note     TEXT,
    diff             TEXT,                     -- unified diff of this one line

    -- --- the gate -----------------------------------------------------------
    checklist        TEXT NOT NULL DEFAULT '[]',  -- [{item, done, done_at}]
    blockers         TEXT NOT NULL DEFAULT '[]',  -- [{code, text, severity, resolved}]
                                                  -- severity 'warn' NEVER refuses an
                                                  -- apply (ruling 8: no headroom floor).
    state            TEXT NOT NULL DEFAULT 'proposed',  -- proposed|testing|applied|rejected
    note             TEXT,

    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE (plan_group, config_path, line)
);

-- No foreign key to `model_units`, for the reason 151 and 152 both declined
-- one: a store rescan can delete a unit row, and a plan that cites a unit which
-- has since gone is exactly what a person needs to SEE. A dangling citation is
-- the finding, not corruption.

CREATE INDEX IF NOT EXISTS idx_model_swap_group ON model_swap_plans(plan_group);
CREATE INDEX IF NOT EXISTS idx_model_swap_state ON model_swap_plans(state);
CREATE INDEX IF NOT EXISTS idx_model_swap_consumer ON model_swap_plans(consumer);
CREATE INDEX IF NOT EXISTS idx_model_swap_unit_old ON model_swap_plans(unit_old);

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP INDEX IF EXISTS idx_model_swap_unit_old;
--   DROP INDEX IF EXISTS idx_model_swap_consumer;
--   DROP INDEX IF EXISTS idx_model_swap_state;
--   DROP INDEX IF EXISTS idx_model_swap_group;
--   DROP TABLE IF EXISTS model_swap_plans;
