-- <!-- AGENT_HEADER
-- role: code
-- purpose: Target-audience registry — first-class groups (board / top-management /
--   engineers / designers …) that a prism deck can be tailored for. Thin entity:
--   membership is resolved by querying affiliations on role_class (+company),
--   with an optional explicit-override join. Empty group → generic deck from the
--   role_class slider preset.
-- index: content
-- AGENT_HEADER_END -->
--
-- Sits on top of migration 056 (companies + affiliations). A "target group" is
-- the audience a deck is built for. Two flavours:
--   * standard template (company_id NULL) — reusable archetype like "board" or
--     "designers", carrying only a slider seed; renders a GENERIC deck.
--   * concrete group (company_id set)      — e.g. board-of-mobiliar; members are
--     the company's active affiliations whose role_class matches, merged into
--     one audience lens.
--
-- role_class is free TEXT validated at the writer against ROLE_DEFAULT_SLIDERS
-- (mirrors affiliations.role_class in 056 — adding a class is a code change, not
-- a schema migration). seed_sliders is a JSON snapshot of the preset at creation
-- so an empty/template group can still render without a live person.

-- 1. Target groups — the audience entity --------------------------------------
CREATE TABLE IF NOT EXISTS target_groups (
    id            TEXT PRIMARY KEY,                       -- slug, e.g. 'board-of-mobiliar'
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'custom'
                  CHECK (kind IN ('standard','custom')),
    company_id    TEXT REFERENCES companies(id) ON DELETE CASCADE,  -- NULL = reusable template
    role_class    TEXT,                                   -- ROLE_DEFAULT_SLIDERS key, validated at writer
    seed_sliders  TEXT,                                   -- JSON snapshot of the preset (generic fallback)
    notes         TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_target_groups_company ON target_groups(company_id);
CREATE INDEX IF NOT EXISTS idx_target_groups_class   ON target_groups(role_class);

-- 2. Explicit member overrides ------------------------------------------------
-- Affiliation-derived membership (role_class match within the company) is the
-- default and is QUERIED, not stored. This join only holds explicit adds — a
-- person pulled into a group who isn't captured by the affiliation query.
CREATE TABLE IF NOT EXISTS target_group_members (
    id          TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL REFERENCES target_groups(id) ON DELETE CASCADE,
    person_id   TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_target_group_member
    ON target_group_members(group_id, person_id);
CREATE INDEX IF NOT EXISTS idx_target_group_members_group
    ON target_group_members(group_id);
