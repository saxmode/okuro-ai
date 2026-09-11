-- Migration 109 — people_graph_*: server-side home for the People-graph
-- arrangement (node positions, groups, view settings).
--
-- WHY. Until now the entire arrangement lived in browser localStorage under
-- `okuro.people-graph.{positions|groups|settings}.v1`
-- (web/frontend/src/components/people/graph-storage.ts). Opening okuro from a
-- second browser or a second machine showed the default layout, because there
-- was no server round-trip at all — no route, no column, nowhere for it to go.
-- These three tables are that missing home. The client keeps localStorage as a
-- synchronous cache; the DB is the source of truth on hydrate.
--
-- WHY NOT A FOREIGN KEY on node_id. The positions map is keyed by React-Flow
-- NODE id, not person id, and the graph has three node kinds: "me" (the user,
-- synthesised in people.py::people_graph, never a persons row), "person"
-- (a persons.id), and group bubbles (`g_<base36>_<rand>`). PeopleGraph.tsx's
-- onNodeDragStop writes `nextPositions[node.id]` for whichever node was
-- dragged, so all three kinds land here. A REFERENCES persons(id) would 403
-- the "me" node and every bubble. Orphan rows (a person deleted while their
-- position remains) are harmless — the client only reads positions for nodes
-- the graph actually returned — and are bounded by the number of people.
--
-- WHY member_ids IS JSON, not a join table. Membership normalisation is the
-- NEXT step: graph groups are slated to merge into the existing `target_groups`
-- / `target_group_members` pair so a group drawn in the UI is also usable as a
-- prism/handover audience lens. Building a third membership table here would
-- create schema that step immediately deletes (DP09). Until then this column
-- mirrors the client's `Group.memberIds` exactly.
--
-- WHY settings IS A KV, not a singleton row with one column. `connectionStyle`
-- is the first graph-view preference, not the last (node size and label density
-- are already discussed). A 2-column KV absorbs those without a migration each;
-- a `connection_style` column would need one every time. Unknown keys are
-- ignored by the reader, so an older frontend tolerates a newer key.
--
-- CARDINALITY. One row per node (positions), one per group, one per setting
-- key. All three are written as WHOLE-COLLECTION REPLACEMENTS by
-- PUT /api/people/layout inside a single transaction, matching the client,
-- which only ever hands over its complete map.
--
-- Rollback (SQLite forward-only):
--   DROP TABLE IF EXISTS people_graph_positions;
--   DROP TABLE IF EXISTS people_graph_groups;
--   DROP TABLE IF EXISTS people_graph_settings;

-- React-Flow node id -> top-left position. Stored as RF top-lefts (NOT visual
-- centres) because that is the coordinate space RF hands back and expects on
-- restore; graph-storage.ts and the group-geometry migration in PeopleGraph.tsx
-- both assume it. Storing centres here would silently offset every node by
-- NODE_HALF on the next load.
CREATE TABLE IF NOT EXISTS people_graph_positions (
    node_id    TEXT PRIMARY KEY,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A user-drawn cluster on the graph. center_x/center_y/radius are the group's
-- STORED identity: the orbit-spring simulation targets the stored centre and
-- drop-detection uses the stored circle, so a group survives losing all its
-- members. Deriving these from live member positions is a known regression
-- (drift, lost clicks, vanishing groups) — keep them stored.
CREATE TABLE IF NOT EXISTS people_graph_groups (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    -- JSON array of persons.id. See "WHY member_ids IS JSON" above.
    member_ids   TEXT NOT NULL DEFAULT '[]',
    center_x     REAL,
    center_y     REAL,
    radius       REAL,
    -- Mirrors GROUP_GEOM_VERSION in graph-storage.ts. The client re-snapshots
    -- geometry for anything below the current version, so this must round-trip
    -- unchanged or that backfill re-runs on every load.
    geom_version INTEGER,
    -- Preserves the user's stacking/ordering across the whole-collection PUT;
    -- without it SQLite row order is unspecified and groups would shuffle.
    sort_order   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_people_graph_groups_order
    ON people_graph_groups (sort_order);

-- Graph-view preferences. Currently one key: connection_style.
CREATE TABLE IF NOT EXISTS people_graph_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
