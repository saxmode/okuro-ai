-- <!-- AGENT_HEADER
-- role: code
-- purpose: Brands + principle sets — project-level composition of design + stacks + constraints.
-- index: content
-- AGENT_HEADER_END -->
-- Brands compose a project's identity from three registries:
--   1. design profiles   (design/profiles/{id}.yaml, file-backed)
--   2. stack profiles    (stack_profiles table, + new scope col)
--   3. principle sets    (new principle_sets table)
--
-- Slots are rows (not columns) so adding a new slot kind (e.g. mobile_stack,
-- content_cms) later is a single INSERT into brand_slot_kinds — no schema
-- migration, no code change downstream, UI picks it up from the kinds table.

-- 1. Scope for stack profiles ------------------------------------------------
-- SQLite lacks ADD COLUMN ... CHECK; we document the allowed set in code
-- (frontend | backend | fullstack | agent | other) and enforce at the writer.
ALTER TABLE stack_profiles ADD COLUMN scope TEXT NOT NULL DEFAULT 'fullstack';

CREATE INDEX IF NOT EXISTS idx_stack_profiles_scope ON stack_profiles(scope);

-- 2. Brand slot kinds (small registry of slot types) ------------------------
CREATE TABLE IF NOT EXISTS brand_slot_kinds (
    kind          TEXT PRIMARY KEY,
    registry      TEXT NOT NULL
                  CHECK (registry IN ('design_profile','stack_profile','principle_set')),
    required      INTEGER NOT NULL DEFAULT 0,
    cardinality   TEXT NOT NULL DEFAULT 'single'
                  CHECK (cardinality IN ('single','multi')),
    scope_filter  TEXT,    -- For stack_profile slots: restrict ref_id to
                           -- stack_profiles.scope = scope_filter. NULL = any.
    sort_order    INTEGER DEFAULT 0,
    description   TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

-- 3. Brands (top-level composition) -----------------------------------------
CREATE TABLE IF NOT EXISTS brands (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','draft','archived')),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

-- 4. Brand slot assignments -------------------------------------------------
-- ref_id is intentionally not FK-constrained: each slot points at a different
-- registry (design files / stack_profiles / principle_sets). Validation lives
-- in stack.validator.lint_brand (reachable via MCP + /api/stack/brands/*/lint).
CREATE TABLE IF NOT EXISTS brand_slots (
    brand_id   TEXT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    slot_kind  TEXT NOT NULL REFERENCES brand_slot_kinds(kind),
    ref_id     TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (brand_id, slot_kind, position)
);

CREATE INDEX IF NOT EXISTS idx_brand_slots_kind_ref
    ON brand_slots(slot_kind, ref_id);

-- 5. Project → brand binding (coexists with stack_project_profile) ----------
-- Precedence at resolve time: brand wins if set, else fall back to profile.
CREATE TABLE IF NOT EXISTS stack_project_brand (
    project_slug  TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    brand_id      TEXT NOT NULL REFERENCES brands(id),
    assigned_at   TEXT DEFAULT (datetime('now'))
);

-- 6. Principle sets ---------------------------------------------------------
-- Named bundles of principle IDs. Where "principles" (DP01…DP10) are atomic
-- decision principles living on the user profile, principle_sets are
-- project-level *constraints* that a brand can compose.
CREATE TABLE IF NOT EXISTS principle_sets (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','draft','archived')),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS principle_set_members (
    set_id        TEXT NOT NULL REFERENCES principle_sets(id) ON DELETE CASCADE,
    principle_id  TEXT NOT NULL REFERENCES principles(id),
    sort_order    INTEGER DEFAULT 0,
    PRIMARY KEY (set_id, principle_id)
);

CREATE INDEX IF NOT EXISTS idx_principle_set_members_principle
    ON principle_set_members(principle_id);
