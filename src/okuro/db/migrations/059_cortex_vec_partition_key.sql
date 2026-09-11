-- Migration 059 — vec_cortex partition key + cosine distance (audit F20/F21/F23).
--
-- The vec_cortex vec0 virtual table is created at RUNTIME by
-- src/okuro/cortex/vectorstore.py:_ensure_schema, not by a migration (vec0
-- tables fix their column shape at CREATE time and CREATE ... IF NOT EXISTS
-- would keep the old shape forever). Two correctness fixes change that shape:
--
--   1. F20/F21 — add a `project TEXT partition key` so a scoped query filters
--      INSIDE the KNN scan. The pre-fix path ran a GLOBAL top-(k*3) scan then
--      filtered project in Python; because one project is ~90% of the index,
--      the minority project's true top-k was buried before Python saw it, so
--      scoped recall collapsed to 0-1/k.
--   2. F23 — declare `distance_metric=cosine`. The table was created with the
--      default L2 metric while the code scored `similarity = 1 - distance`,
--      which goes negative once distance > 1. Native cosine distance is
--      [0, 2] and the code now maps it via `1 - distance/2`.
--
-- New shape (built by _ensure_schema on next startup, VEC_CORTEX_SCHEMA_VERSION=2):
--   vec0(id TEXT PRIMARY KEY, project TEXT partition key,
--        embedding float[<dim>] distance_metric=cosine)
--
-- SHAPE-AWARE, CONDITIONAL reshape (zero-outage on an already-v2 install).
-- This migration does NOT drop vec_cortex. It only sets a "reshape pending"
-- marker WHEN a genuine v1 (old-shape) table exists — detected by the absence
-- of `partition key` in the table's own DDL (sqlite_master.sql). The actual
-- drop+recreate is performed by the runtime _ensure_schema, which is also
-- shape-aware:
--   * v2 table (DDL already has `partition key`)  → PRESERVED, never dropped,
--     even when populated → NO re-embed, NO outage. This is the live install:
--     its vec_cortex is already v2, so applying 056 is a no-op for the data.
--   * v1 table + marker set (or empty)            → dropped + recreated v2.
--   * v1 table + no marker + populated            → left intact; search
--     tolerates it via a global-scan fallback until a deliberate reshape.
--
-- Why a marker instead of dropping here: vec0 is a virtual table created at
-- RUNTIME by _ensure_schema (migrations can't portably CREATE it at the right
-- dim), so the deliberate reshape must happen there. The marker is the
-- migration's one-way signal "a v1 table was present at upgrade time, reshape
-- it". An unconditional DROP here would needlessly nuke a correct v2 index and
-- force a full re-embed/outage — exactly what this revision prevents.
--
-- ONE-TIME COST (only for a genuine v1 install): the runtime drop discards the
-- old-shape vectors, forcing a FULL RE-EMBED on the next reindex. cortex_docs
-- is LEFT INTACT; index_file() re-embeds any doc whose vec row is MISSING, so
-- the standard refresh_cortex / `okuro cortex refresh` repopulates without a
-- forced full rescan. A v2 install pays NONE of this. This migration does NO
-- embedding — runtime work.

-- Set the reshape-pending marker ONLY when a v1-shape vec_cortex exists.
INSERT OR REPLACE INTO cortex_meta(key, value)
SELECT 'vec_cortex_reshape_pending', '1'
WHERE EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type = 'table' AND name = 'vec_cortex'
      AND lower(sql) NOT LIKE '%partition key%'
);

-- Clear the schema-version marker so _ensure_schema re-evaluates the shape and
-- re-stamps v2 once the live table actually has the partitioned/cosine DDL.
DELETE FROM cortex_meta WHERE key = 'vec_cortex_schema_version';

-- _ensure_schema (shape-aware) does the deliberate drop+recreate iff the
-- reshape-pending marker is set on a v1 table, then repopulation happens via
-- the normal reindex. A v2 table is preserved untouched.
