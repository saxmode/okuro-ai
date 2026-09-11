-- <!-- AGENT_HEADER
-- role: code
-- purpose: 049_knowledge_saved_queries — bookmarked filter views for /knowledge.
-- index: content
-- AGENT_HEADER_END -->
--
-- Saved queries are named filter chips above the FacetSidebar. Each row is
-- one "lens" the user has bookmarked into the unified knowledge graph
-- (e.g. "okuro gotchas", "decisions across all projects").
--
-- filter_dsl is a JSON blob mirroring the TS FilterState shape — kept
-- opaque on the server side so the frontend can evolve the filter
-- vocabulary without a schema migration. last_used_at orders the
-- chip row so the most-recently-used bookmark sits first.

CREATE TABLE IF NOT EXISTS knowledge_saved_queries (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    filter_dsl    TEXT NOT NULL DEFAULT '{}',
    color_group   TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    last_used_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_knowledge_saved_queries_last_used
    ON knowledge_saved_queries(last_used_at DESC);

CREATE TRIGGER IF NOT EXISTS knowledge_saved_queries_reject_empty_name
BEFORE INSERT ON knowledge_saved_queries
FOR EACH ROW
WHEN NEW.name IS NULL OR LENGTH(TRIM(NEW.name)) = 0
BEGIN
    SELECT RAISE(ABORT, 'knowledge_saved_queries.name must be non-empty');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_saved_queries_touch_updated_at
AFTER UPDATE ON knowledge_saved_queries
FOR EACH ROW
BEGIN
    UPDATE knowledge_saved_queries SET updated_at = datetime('now') WHERE id = OLD.id;
END;
