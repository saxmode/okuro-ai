-- <!-- AGENT_HEADER
-- role: code
-- purpose: 039_cortex_meta_embedding — record which embedding tier the
--   current vec_cortex shape was built with, so the API switch-tier
--   handler can detect dim changes and trigger a re-embed.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a meta table:
--   The vec_cortex virtual table fixes its column dimension at CREATE
--   time. Without a separate record of "which tier produced the current
--   shape", a tier switch can't tell whether a re-embed is required.
--   This migration adds cortex_meta(key, value) — a tiny KV store —
--   and seeds the active tier + dim from the env-var/config fallback
--   so existing installs converge on the right values without a forced
--   re-embed.
--
-- Re-embed trigger:
--   On user-driven tier switch (orchestrator/api ... /embed PUT), the
--   handler compares the new tier's dim against cortex_meta.embedding_dim.
--   If they differ: DROP TABLE vec_cortex; DELETE FROM cortex_docs;
--   then enqueue a daemon reindex job. WS events
--   cortex.reindex.{started,progress,done} stream progress to the UI.

CREATE TABLE IF NOT EXISTS cortex_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed values are populated by the runtime on first vectorstore init —
-- can't be SQL-only because the active tier is only resolvable from
-- env/config at runtime. The runtime upserts:
--   ('embedding_tier', 'low'|'high')
--   ('embedding_dim',  '768'|'1024')
--   ('last_reindex_at', ISO-8601 UTC timestamp)
