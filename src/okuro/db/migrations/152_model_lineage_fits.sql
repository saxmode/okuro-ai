-- <!-- AGENT_HEADER
-- role: code
-- purpose: 152_model_lineage_fits — P3 of okuro-model-manager. Three things
--   that did not exist anywhere in okuro before: what LINE a model belongs to
--   (family/version/size/quant/variant), whether a discovered RELEASE is an
--   update of something already installed, and WHERE on this host's hardware a
--   model could actually run.
--   Migration 150 deliberately left family/version/variant off `model_units`
--   with the note "lineage is P3 and it needs a matcher, not a column". The
--   matcher is `okuro.ai_models.lineage`; these are its columns.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, idempotent at the migration-ledger level (each file runs once).
-- Nothing here is populated by the migration: the unit columns are written by
-- lineage.persist_unit_lineage and the fits by fitting.persist_fits, both of
-- which are no-ops unless the host declares `ai_models.model_stores`.
--
-- WHY LINEAGE COLUMNS AND NOT A LINEAGE TABLE. A unit has exactly one family,
-- one version, one quant — it is a property of the unit, not a relation to
-- something else. A side table would buy a join and an opportunity for the two
-- to disagree. The RELATION, which genuinely is between two things, lives on
-- `model_discoveries` where the candidate already has a row.
--
-- WHY THE PARSE IS STORED AT ALL rather than recomputed on read. Two reasons,
-- both operational: the inventory page joins on family and cannot run a Python
-- parser inside SQL, and a stored parse is auditable — when a name parses
-- wrongly the row shows what was believed, which is what makes the error
-- findable instead of merely wrong.

-- --- model_units: what line is this unit from -------------------------------
ALTER TABLE model_units ADD COLUMN family TEXT;
ALTER TABLE model_units ADD COLUMN version TEXT;
ALTER TABLE model_units ADD COLUMN params_total_b REAL;
ALTER TABLE model_units ADD COLUMN params_active_b REAL;   -- MoE: NULL when dense
ALTER TABLE model_units ADD COLUMN quant TEXT;             -- as written: i1-Q6_K, UD-Q3_K_XL
ALTER TABLE model_units ADD COLUMN variant_tags TEXT;      -- JSON array, never NULL-as-empty
ALTER TABLE model_units ADD COLUMN author TEXT;            -- publisher or quanter

CREATE INDEX IF NOT EXISTS idx_model_units_family ON model_units(family, version);

-- --- model_discoveries: is this release an update of something here ---------
-- `relation` is NULL until the lineage pass has seen the row, which is a
-- different fact from 'unrelated' — one means not yet asked, the other means
-- asked and the answer is no.
ALTER TABLE model_discoveries ADD COLUMN family TEXT;
ALTER TABLE model_discoveries ADD COLUMN version TEXT;
ALTER TABLE model_discoveries ADD COLUMN params_total_b REAL;
ALTER TABLE model_discoveries ADD COLUMN params_active_b REAL;
ALTER TABLE model_discoveries ADD COLUMN quant TEXT;
ALTER TABLE model_discoveries ADD COLUMN variant_tags TEXT;
ALTER TABLE model_discoveries ADD COLUMN relation TEXT;
ALTER TABLE model_discoveries ADD COLUMN relation_target TEXT;   -- unit_id, no FK: see below
ALTER TABLE model_discoveries ADD COLUMN relation_confidence REAL;

CREATE INDEX IF NOT EXISTS idx_model_disc_family   ON model_discoveries(family, version);
CREATE INDEX IF NOT EXISTS idx_model_disc_relation ON model_discoveries(relation);

-- --- model_fits: where could this run on THIS host --------------------------
-- One row per (subject, placement mode, GPU set). Ruling 6 rejected a single
-- per-GPU size check as too simple and asked for the PLACEMENT SPACE: a model
-- that does not fit one card may still run split across two, or with its cold
-- experts in system RAM, and "does it run here" is the OR of those, not the
-- answer for one card.
--
-- subject_kind = 'unit'    -> subject_id is a model_units.unit_id
-- subject_kind = 'release' -> subject_id is a model_discoveries.catalog_id
-- No foreign key on purpose, and for the same reason 151 declined one: a store
-- rescan can delete a unit row, and a fit computed for something that has gone
-- is stale rather than corrupt. The scan deletes its own stale fits.
--
-- fits_idle vs fits_now is the whole contention story in two columns. idle is
-- "if the GPUs were empty"; now subtracts what is resident at computed_at
-- (okuro's broker ledger plus live nvidia-smi free). A fit that is true idle
-- and false now is not a failure — it is a queue.
CREATE TABLE IF NOT EXISTS model_fits (
    subject_kind  TEXT NOT NULL CHECK (subject_kind IN ('unit', 'release')),
    subject_id    TEXT NOT NULL,
    mode          TEXT NOT NULL,          -- single-gpu | gpu-split | gpu-ram-offload | moe-expert-offload | cpu-only | media-*
    -- The GPU's configured name, or several joined by '+' for a split, or ''
    -- for cpu-only. Names come from the host's `conventions.gpus` at runtime
    -- and are never written into this repo.
    gpu           TEXT NOT NULL DEFAULT '',
    est_vram_gb   REAL NOT NULL DEFAULT 0,   -- total across the GPUs in `gpu`
    vram_detail   TEXT,                      -- JSON {gpu_name: gb} — a split's whole point
    est_ram_gb    REAL NOT NULL DEFAULT 0,
    offloaded_pct REAL NOT NULL DEFAULT 0,
    speed_class   TEXT NOT NULL DEFAULT 'slow'
                    CHECK (speed_class IN ('fast', 'usable', 'slow')),
    fits_idle     INTEGER NOT NULL DEFAULT 0,
    fits_now      INTEGER NOT NULL DEFAULT 0,
    basis         TEXT,                      -- gguf-header | param-heuristic | media-estimate
    why           TEXT,                      -- one line, why this number
    computed_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subject_kind, subject_id, mode, gpu)
);

CREATE INDEX IF NOT EXISTS idx_model_fits_subject ON model_fits(subject_kind, subject_id);
CREATE INDEX IF NOT EXISTS idx_model_fits_runnable ON model_fits(fits_idle, speed_class);

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP INDEX IF EXISTS idx_model_fits_runnable;
--   DROP INDEX IF EXISTS idx_model_fits_subject;
--   DROP TABLE IF EXISTS model_fits;
--   DROP INDEX IF EXISTS idx_model_disc_relation;
--   DROP INDEX IF EXISTS idx_model_disc_family;
--   DROP INDEX IF EXISTS idx_model_units_family;
--   ALTER TABLE model_discoveries DROP COLUMN relation_confidence;
--   ALTER TABLE model_discoveries DROP COLUMN relation_target;
--   ALTER TABLE model_discoveries DROP COLUMN relation;
--   ALTER TABLE model_discoveries DROP COLUMN variant_tags;
--   ALTER TABLE model_discoveries DROP COLUMN quant;
--   ALTER TABLE model_discoveries DROP COLUMN params_active_b;
--   ALTER TABLE model_discoveries DROP COLUMN params_total_b;
--   ALTER TABLE model_discoveries DROP COLUMN version;
--   ALTER TABLE model_discoveries DROP COLUMN family;
--   ALTER TABLE model_units DROP COLUMN author;
--   ALTER TABLE model_units DROP COLUMN variant_tags;
--   ALTER TABLE model_units DROP COLUMN quant;
--   ALTER TABLE model_units DROP COLUMN params_active_b;
--   ALTER TABLE model_units DROP COLUMN params_total_b;
--   ALTER TABLE model_units DROP COLUMN version;
--   ALTER TABLE model_units DROP COLUMN family;
