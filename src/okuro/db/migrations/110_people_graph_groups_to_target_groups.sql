-- Migration 110 — fold People-graph groups into target_groups.
--
-- WHY. Migration 109 gave the graph's groups a server-side home, but a private
-- one: `people_graph_groups` was a second, parallel grouping concept that no
-- other part of okuro could see. Drawing a board group on the graph produced
-- a cluster the prism generator, the handover recipient picker and every MCP
-- agent were blind to, while `target_groups` already held the SAME idea — a
-- named set of people you address as one audience — and was wired into all of
-- them. Two concepts for one thing is the defect (DP10); this migration deletes
-- the duplicate rather than teaching everything to read both.
--
-- A hand-drawn graph group maps onto the existing model as a CUSTOM group with
-- company_id NULL, role_class NULL and explicit members: peer/target_groups.py
-- ::_member_ids returns affiliation-derived ∪ explicit, so with no company and
-- no role_class it returns exactly the people the user dropped into the circle.
-- resolve_audience then merges their cognitive lenses with no further work —
-- that payoff is the entire point of this migration.
--
-- WHY GEOMETRY IS A SIDE TABLE, not four columns on target_groups. Most target
-- groups are NOT on the graph: the seven seeded templates (board, engineers, …)
-- have no position and never will, and a group created through the MCP tool has
-- none either. Columns would put four NULLs on every such row and imply every
-- audience has a place on a canvas. A sparse side table says the true thing,
-- and its presence becomes the predicate the API needs anyway:
--
--     a target group is ON the People graph  ⟺  it has a geometry row
--
-- That predicate is load-bearing. PUT /api/people/layout replaces the whole
-- collection, so it must know which groups it is allowed to delete; without it,
-- saving the graph would wipe the standard templates and every MCP-created
-- audience, none of which the client ever sends.
--
-- WHY THE CLIENT'S `g_<base36>_<rand>` IDS SURVIVE rather than being slugified
-- into the target_groups convention. Two reasons, either sufficient:
--   1. people_graph_positions is keyed by React-Flow NODE id, and a group's
--      bubble IS a node — its id appears in that table. Rewriting group ids
--      would orphan every bubble's stored position.
--   2. slugify(name) collides. A user drawing a group called "Board" would
--      produce id 'board' and the upsert in target_group_add would silently
--      overwrite the seeded standard template.
-- target_group_add already accepts an explicit group_id, so an opaque id is a
-- supported shape here, not a smuggled one.
--
-- Rollback (SQLite forward-only) — recreate 109's table and copy back:
--   CREATE TABLE people_graph_groups (
--       id TEXT PRIMARY KEY, name TEXT NOT NULL,
--       member_ids TEXT NOT NULL DEFAULT '[]',
--       center_x REAL, center_y REAL, radius REAL, geom_version INTEGER,
--       sort_order INTEGER NOT NULL DEFAULT 0,
--       updated_at TEXT NOT NULL DEFAULT (datetime('now')));
--   INSERT INTO people_graph_groups
--     SELECT g.id, g.name,
--            (SELECT json_group_array(m.person_id) FROM target_group_members m
--              WHERE m.group_id = g.id),
--            gm.center_x, gm.center_y, gm.radius, gm.geom_version,
--            gm.sort_order, gm.updated_at
--       FROM target_groups g JOIN people_graph_group_geometry gm ON gm.group_id = g.id;
--   DROP TABLE people_graph_group_geometry;

-- Where a target group sits on the People graph. One row per group that has
-- been placed on the canvas; absent for templates and MCP-created audiences.
--
-- center/radius are the group's STORED identity, not derived from members: the
-- orbit-spring targets the stored centre and drop-detection uses the stored
-- circle, so a group survives losing every member. Deriving them from live
-- member positions is a known regression (drift, lost clicks, vanishing
-- groups) — carried over verbatim from migration 109.
CREATE TABLE IF NOT EXISTS people_graph_group_geometry (
    group_id     TEXT PRIMARY KEY
                 REFERENCES target_groups(id) ON DELETE CASCADE,
    center_x     REAL,
    center_y     REAL,
    radius       REAL,
    -- Mirrors GROUP_GEOM_VERSION in graph-storage.ts. Must round-trip
    -- unchanged or the client's geometry backfill re-runs on every load.
    geom_version INTEGER,
    -- Preserves the user's group ordering across a whole-collection PUT;
    -- SQL row order is otherwise unspecified and groups would shuffle.
    sort_order   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_people_graph_group_geometry_order
    ON people_graph_group_geometry (sort_order);

-- ── Carry 109's rows across ─────────────────────────────────────────────────
-- Guarded by the table's existence at author time; on a fresh install
-- people_graph_groups exists (109 runs first) and is simply empty.

-- 1. The groups themselves. kind='custom', company_id/role_class NULL — the
--    "explicit members only" shape _member_ids resolves correctly.
INSERT OR IGNORE INTO target_groups (id, name, kind, company_id, role_class, notes)
SELECT id, name, 'custom', NULL, NULL, 'Drawn on the People graph'
  FROM people_graph_groups;

-- 2. Membership, exploded from the JSON array. Joined against persons because
--    target_group_members.person_id is a FK — a stale id in the JSON (person
--    deleted while the group kept their id) would abort the migration.
INSERT OR IGNORE INTO target_group_members (id, group_id, person_id)
SELECT 'tgm_' || lower(hex(randomblob(6))), g.id, p.id
  FROM people_graph_groups g
  JOIN json_each(g.member_ids) j
  JOIN persons p ON p.id = j.value;

-- 3. Geometry.
INSERT OR IGNORE INTO people_graph_group_geometry
    (group_id, center_x, center_y, radius, geom_version, sort_order, updated_at)
SELECT id, center_x, center_y, radius, geom_version, sort_order, updated_at
  FROM people_graph_groups;

DROP TABLE IF EXISTS people_graph_groups;
