-- <!-- AGENT_HEADER
-- role: code
-- purpose: 064_flow_designer — persistence for okuro-flow, the generic node-graph
--   canvas (ported from tools/flow-designer). Independent of the legacy star-topology
--   `okuro.flows` YAML store — different feature, different table.
-- index: content
-- AGENT_HEADER_END -->
--
-- okuro-flow is a free-form ReactFlow diagram editor surfaced in okuro.web at /flow
-- and writable by agents via the flow_designer_* MCP tools. The whole graph
-- (nodes + edges + viewport + settings) is stored as one JSON blob in `graph`;
-- node_count / edge_count are denormalised for cheap list rendering.
--
-- `flow_designer_events` is an append-only change-feed. Both the web process
-- (user edits) and the stdio MCP process (agent edits) write to it on every
-- save/delete. The web SSE endpoint (/api/flow-designer/events) tails it by `seq`
-- so an open canvas live-updates when ANY process mutates a flow — the only
-- cross-process-safe realtime channel given the SQLite backend. `origin` carries
-- the writer's client id so a client can ignore the echo of its own write.

CREATE TABLE IF NOT EXISTS flow_designer (
    id TEXT PRIMARY KEY,                                  -- slug, unique
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    graph TEXT NOT NULL DEFAULT '{"nodes":[],"edges":[]}',-- JSON {nodes,edges,viewport?,settings?}
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_flow_designer_updated ON flow_designer(updated_at DESC);

CREATE TABLE IF NOT EXISTS flow_designer_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('saved', 'deleted')),
    origin TEXT NOT NULL DEFAULT '',                      -- writer client id (echo suppression)
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_flow_designer_events_seq ON flow_designer_events(seq);
