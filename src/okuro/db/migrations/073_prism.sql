-- <!-- AGENT_HEADER
-- role: code
-- purpose: 073_prism — persistence for okuro·prism, the multi-depth
--   progressive-disclosure comms tool. Mirrors slides (067): one JSON blob
--   per doc + an append-only change-feed for cross-process live sync.
-- index: content
-- AGENT_HEADER_END -->
--
-- A PrismDoc (title, brand_id, entry_facet_id, facets{}) is stored as one JSON
-- blob in `doc`. `brand_id` and `facet_count` are denormalised columns (cheap
-- list rendering, brand filtering). Unlike slides (variantOf lives inside the
-- deck blob), prism's retailor lineage is a first-class column so variant
-- chains are queryable without loading every doc's JSON.
--
-- `prism_events` is the append-only change-feed: both the web process (user
-- edits) and the stdio MCP process (agent / chat edits) write to it on every
-- save/delete, and the web SSE endpoint tails it by `seq` so an open /prism
-- canvas live-updates when ANY process mutates it. `origin` carries the
-- writer's client id so a client can ignore the echo of its own write.

CREATE TABLE IF NOT EXISTS prism_docs (
    id TEXT PRIMARY KEY,                         -- slug, unique
    title TEXT NOT NULL,
    brand_id TEXT,
    variant_of TEXT REFERENCES prism_docs(id),   -- retailor variant lineage
    doc TEXT NOT NULL DEFAULT '{}',              -- JSON: full PrismDoc (facets + entry_facet_id)
    facet_count INTEGER NOT NULL DEFAULT 0,      -- denormalised, cheap list rendering
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prism_docs_updated ON prism_docs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_prism_docs_variant_of ON prism_docs(variant_of);

CREATE TABLE IF NOT EXISTS prism_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('saved', 'deleted')),
    origin TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prism_events_seq ON prism_events(seq);
