-- <!-- AGENT_HEADER
-- role: code
-- purpose: 032_memory_tunnels — cross-project memory tunnels (mempalace palace-graph pattern).
-- index: content
-- AGENT_HEADER_END -->
--
-- Memories are project-scoped today, so a learning written under project A
-- ('MCP middleware bug') is invisible when an agent later works in project B.
-- Tunnels solve this by linking memories to free-form concept tags. A query
-- by concept surfaces every linked memory regardless of project — okuro's
-- equivalent of mempalace's wing-bridging tunnels.
--
-- (concept, memory_id) is unique — same memory can carry multiple concepts
-- but never the same concept twice.

CREATE TABLE IF NOT EXISTS memory_tunnels (
    id          TEXT PRIMARY KEY,
    concept     TEXT NOT NULL,
    memory_id   TEXT NOT NULL REFERENCES agent_memory(id) ON DELETE CASCADE,
    project     TEXT REFERENCES projects(id),
    note        TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_tunnels_concept ON memory_tunnels(concept);
CREATE INDEX IF NOT EXISTS idx_memory_tunnels_memory  ON memory_tunnels(memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_tunnels_project ON memory_tunnels(project);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_tunnels_concept_memory
    ON memory_tunnels(concept, memory_id);
