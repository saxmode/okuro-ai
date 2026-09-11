-- Migration 038 — embedding model swap: bge-small (384d) → Qwen3-Embedding-0.6B (1024d).
--
-- The vec_cortex virtual table fixes its column dimension at CREATE time.
-- Code in src/okuro/cortex/vectorstore.py:_ensure_schema uses
-- CREATE VIRTUAL TABLE IF NOT EXISTS, so the old 384-dim shape would
-- survive a `git pull` and every new INSERT would error out with a
-- dim-mismatch (1024d vector → float[384] column).
--
-- We drop vec_cortex AND clear cortex_docs so the next index_directory
-- walk actually re-embeds: index_file's hash check returns False ("file
-- already indexed, content unchanged") for any row that survives — even
-- though the underlying vector would now be the wrong dimension.
-- Clearing cortex_docs forces a full re-embed.
--
-- Re-index trigger: the daemon's refresh_cortex task runs on its normal
-- schedule (or the user can `okuro cortex refresh` immediately). The
-- migration itself does NOT call out to the embed service — that work
-- belongs to the runtime.
--
-- okuro-embed must restart to pick up the new model id. update.sh
-- already restarts the three okuro-* services when migrations change.

DROP TABLE IF EXISTS vec_cortex;
DELETE FROM cortex_docs;

-- _ensure_schema recreates vec_cortex with the new dim on next startup.
