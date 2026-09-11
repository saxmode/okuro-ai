-- <!-- AGENT_HEADER
-- role: code
-- purpose: 065_flow_designer_occ — optimistic concurrency + durability for okuro-flow.
--   Adds a monotonic `rev` to flow_designer (every save bumps it) and a
--   `flow_designer_history` snapshot table (every overwrite snapshots the prior
--   graph). Together these kill the last-writer-wins clobber: a writer holding a
--   stale `rev` is rejected (409), and any accepted overwrite stays recoverable.
-- index: content
-- AGENT_HEADER_END -->
--
-- Root-cause fix, not a patch. The original store let ANY writer (browser tab,
-- second tab, stdio MCP agent) blind-overwrite the graph with whatever was on
-- its canvas — an empty/stale canvas could erase agent-authored content. `rev`
-- turns every update into a compare-and-swap: clients send the rev they loaded
-- as `base_rev`; the server rejects the write if the stored rev moved on.
-- `flow_designer_history` is the durability backstop — even a legitimate
-- overwrite (e.g. user clears the canvas) snapshots the previous graph so it
-- can be restored.

ALTER TABLE flow_designer ADD COLUMN rev INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS flow_designer_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id TEXT NOT NULL,
    graph TEXT NOT NULL,                                 -- snapshot of the PRIOR graph blob
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    rev INTEGER NOT NULL DEFAULT 0,                       -- rev of the snapshotted (prior) version
    origin TEXT NOT NULL DEFAULT '',                     -- writer that triggered the overwrite
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_flow_designer_history_flow
    ON flow_designer_history(flow_id, seq DESC);
