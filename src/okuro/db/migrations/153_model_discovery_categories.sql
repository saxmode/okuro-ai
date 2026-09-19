-- <!-- AGENT_HEADER
-- role: code
-- purpose: 153_model_discovery_categories — P4 of okuro-model-manager. Two
--   things the weekly release scan could not say before: WHICH wanted category
--   a candidate came from, and whether it is worth the user's attention —
--   computed from the lineage relation and the placement fit migration 152
--   added, never guessed. Plus the per-category scan ledger, so a category
--   that found nothing says so instead of going quiet.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive. Nothing here is populated by the migration: `category`,
-- `interesting` and `interesting_why` are written by
-- `okuro.ai_models.suggest.publish`, and `model_category_scans` by
-- `suggest.run_suggestions`.
--
-- WHY `interesting` IS STORED AND NOT COMPUTED ON READ. It is a function of
-- three things that each move on their own schedule: the relation to what is
-- installed, the placement fit, and whether the category has any installed
-- units at all. Recomputing on read would make the Discover page disagree with
-- the morning brief that quoted it six hours earlier, and would need the whole
-- inventory joined in to render one badge. Stored, it is also auditable —
-- `interesting_why` records the reason that was true at scan time.

-- --- model_discoveries: which bucket, and is it worth a look ----------------
-- `interesting` is NULL until the P4 pass has judged the row, which is a
-- different fact from 0 — one means not yet asked, the other means asked and
-- the answer is no. Same rule 152 set for `relation`.
ALTER TABLE model_discoveries ADD COLUMN category TEXT;
ALTER TABLE model_discoveries ADD COLUMN interesting INTEGER;
ALTER TABLE model_discoveries ADD COLUMN interesting_why TEXT;

CREATE INDEX IF NOT EXISTS idx_model_disc_category ON model_discoveries(category);
CREATE INDEX IF NOT EXISTS idx_model_disc_interesting ON model_discoveries(interesting);

-- --- model_category_scans: what each wanted category found last run --------
-- The user asked for coverage of ten categories. Coverage is a claim that can
-- only be checked if a category that yields nothing SAYS so: an empty Discover
-- list is ambiguous between "nothing was released" and "the scanner for this
-- bucket is broken", and those need different actions. One row per category,
-- replaced each run, carrying the counts that separate the two.
--
-- No foreign key and no history: this is the latest state of ten buckets, not
-- a time series. `scanned_at` is what makes a stale row visible.
CREATE TABLE IF NOT EXISTS model_category_scans (
    category     TEXT PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT '',   -- hf | civitai
    selector     TEXT,                       -- the pipeline tag / type used
    fetched      INTEGER NOT NULL DEFAULT 0, -- candidates the source returned
    passed_gate  INTEGER NOT NULL DEFAULT 0, -- survived rank(strict=True)
    published    INTEGER NOT NULL DEFAULT 0, -- rows written to model_discoveries
    interesting  INTEGER NOT NULL DEFAULT 0,
    zero_result  INTEGER NOT NULL DEFAULT 0, -- 1 = no reputable release this run
    note         TEXT,
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP TABLE IF EXISTS model_category_scans;
--   DROP INDEX IF EXISTS idx_model_disc_interesting;
--   DROP INDEX IF EXISTS idx_model_disc_category;
--   ALTER TABLE model_discoveries DROP COLUMN interesting_why;
--   ALTER TABLE model_discoveries DROP COLUMN interesting;
--   ALTER TABLE model_discoveries DROP COLUMN category;
