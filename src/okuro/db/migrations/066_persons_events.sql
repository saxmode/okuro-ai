-- <!-- AGENT_HEADER
-- role: code
-- purpose: 066_persons_events — append-only change-feed for person records so the
--   People page live-updates when ANY process (web UI OR the stdio MCP agent)
--   adds/updates/deletes a person. Mirrors flow_designer_events (migration 064).
-- index: content
-- AGENT_HEADER_END -->
--
-- `persons_events` is the cross-process realtime channel for the people surface.
-- Every write path appends a row: person_add/person_update (okuro.peer.persons,
-- shared by MCP + web create) and the web update/delete endpoints (which write
-- directly). The web SSE endpoint (/api/people/events) tails it by `seq` so an
-- open People page refetches when a person changes — including agent writes from
-- the separate stdio MCP process, the only cross-process-safe channel on SQLite.
-- `origin` carries the writer ('agent' | 'web') for the live-update flash text.

CREATE TABLE IF NOT EXISTS persons_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('added', 'updated', 'deleted')),
    origin TEXT NOT NULL DEFAULT '',                      -- writer ('agent' | 'web')
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_persons_events_seq ON persons_events(seq);
