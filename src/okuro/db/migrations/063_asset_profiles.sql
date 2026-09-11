-- <!-- AGENT_HEADER
-- role: code
-- purpose: Asset profiles — the brand "assets" leg (icons now, illustrations later).
-- index: content
-- AGENT_HEADER_END -->
-- Adds the third brand leg alongside design + stacks: ASSETS.
--
--   brand = design system (design_profile)
--         + stack          (stack_profile fe/be)
--         + principles     (principle_set)
--         + assets         (asset_profile)   ← this migration
--
-- An asset_profile names WHICH asset provider + WHICH sets + license tier +
-- delivery method a brand uses. The icon LIBRARY itself (35k SVGs + vectors)
-- lives in a separate, licensed, data-dir SQLite file — never in this DB and
-- never in the repo. This table holds only the small per-brand binding config.

-- 1. Expand brand_slot_kinds.registry CHECK to allow 'asset_profile'.
--    SQLite cannot ALTER a CHECK constraint, so rebuild the table. FK from
--    brand_slots(slot_kind) references it by name; preserved across rename.
PRAGMA foreign_keys=OFF;

CREATE TABLE brand_slot_kinds_new (
    kind          TEXT PRIMARY KEY,
    registry      TEXT NOT NULL
                  CHECK (registry IN ('design_profile','stack_profile','principle_set','asset_profile')),
    required      INTEGER NOT NULL DEFAULT 0,
    cardinality   TEXT NOT NULL DEFAULT 'single'
                  CHECK (cardinality IN ('single','multi')),
    scope_filter  TEXT,
    sort_order    INTEGER DEFAULT 0,
    description   TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

INSERT INTO brand_slot_kinds_new
    (kind, registry, required, cardinality, scope_filter, sort_order, description, created_at)
SELECT kind, registry, required, cardinality, scope_filter, sort_order, description, created_at
FROM brand_slot_kinds;

DROP TABLE brand_slot_kinds;
ALTER TABLE brand_slot_kinds_new RENAME TO brand_slot_kinds;

PRAGMA foreign_keys=ON;

-- 2. Asset profiles (scalars). allowed_sets live in asset_profile_sets.
CREATE TABLE IF NOT EXISTS asset_profiles (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    icon_provider  TEXT NOT NULL DEFAULT 'okuro-assets.icons',
    license_tier   TEXT NOT NULL DEFAULT 'permissive'
                   CHECK (license_tier IN ('permissive','licensed-seat')),
    delivery       TEXT NOT NULL DEFAULT 'npm-name'
                   CHECK (delivery IN ('npm-name','inline-svg','sprite')),
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','draft','archived')),
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);

-- 3. Allowed icon sets per profile (the set allow-list a brand may draw from).
--    set_id matches the icon library's set ids (e.g. 'lucide','tabler','sl-ultimate').
CREATE TABLE IF NOT EXISTS asset_profile_sets (
    profile_id  TEXT NOT NULL REFERENCES asset_profiles(id) ON DELETE CASCADE,
    set_id      TEXT NOT NULL,
    sort_order  INTEGER DEFAULT 0,
    PRIMARY KEY (profile_id, set_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_profile_sets_set
    ON asset_profile_sets(set_id);

-- 4. Data seed for EXISTING installs ----------------------------------------
-- seed_registry() only runs on a fresh (empty) DB, so stack.yaml additions do
-- NOT reach an already-seeded DB. Migrations are the upgrade path: seed the
-- new 'assets' slot kind + the architecture-noir-assets profile + the
-- the-machine-internal binding here. All idempotent; on a fresh install
-- seed_registry() later overwrites these from YAML (same values).

INSERT OR REPLACE INTO brand_slot_kinds
    (kind, registry, required, cardinality, scope_filter, sort_order, description)
VALUES
    ('assets', 'asset_profile', 0, 'single', NULL, 50,
     'Asset language — icon/illustration provider, allowed sets, license tier, delivery.');

INSERT OR REPLACE INTO asset_profiles
    (id, name, description, icon_provider, license_tier, delivery, status)
VALUES
    ('architecture-noir-assets', 'Architecture Noir — Assets',
     'Monochrome stroke icons for The Machine''s dark internal tooling. Lucide + Tabler only (MIT, safe to ship); excludes colorful licensed sets to honor the noir restrained-UI constraint.',
     'okuro-assets.icons', 'permissive', 'npm-name', 'active');

INSERT OR REPLACE INTO asset_profile_sets (profile_id, set_id, sort_order) VALUES
    ('architecture-noir-assets', 'lucide', 0),
    ('architecture-noir-assets', 'tabler', 1);

-- Bind to the-machine-internal ONLY if that brand already exists (i.e. the DB
-- was previously seeded). On a fresh-from-migration DB the brands table is
-- empty and seed_registry() will add the binding from YAML instead.
INSERT OR IGNORE INTO brand_slots (brand_id, slot_kind, ref_id, position)
SELECT 'the-machine-internal', 'assets', 'architecture-noir-assets', 0
WHERE EXISTS (SELECT 1 FROM brands WHERE id = 'the-machine-internal');
