-- <!-- AGENT_HEADER
-- role: code
-- purpose: Voice profiles — the brand "voice" leg, split out of the design profile where it never belonged.
-- index: content
-- AGENT_HEADER_END -->
-- Adds the fifth brand leg:
--
--   brand = design system (design_profile)   ← visual only, from here on
--         + stack          (stack_profile fe/be)
--         + principles     (principle_set)
--         + assets         (asset_profile)
--         + VOICE          (voice_profile)   ← this migration
--
-- WHY, in the owner's words on 2026-09-03: "language belongs to the brand not
-- the design."
--
-- v0's design profile fused two unrelated things. Its `visual` and
-- `design_system` blocks are a design system; its `language` block — tone,
-- locale, casing, terminology, voice — and its `brand.owner` / `brand.domain`
-- are a BRAND's voice, and were only ever in the design profile because that
-- was the file that existed. The `design` slot kind still describes itself as
-- "Visual language — tokens, typography, constraints", and constraints and
-- language were never visual.
--
-- The cost of the conflation was concrete: sense/bootstrap/sections.py's
-- build_design_profile reads `language` out of a DESIGN profile, and that block
-- appears in every agent packet in the system. Retiring v0 would have taken the
-- voice of every brand with it, silently, because nothing else carries it.
--
-- SCOPE NOTE, so the next person does not over-read this table: it holds UI
-- VOICE — how a surface talks. Positioning, audience and core values are a
-- different concern again and deliberately have no column here; a brand-values
-- registry, if it is ever wanted, is its own leg rather than more columns on
-- this one.

-- 1. Expand brand_slot_kinds.registry CHECK to allow 'voice_profile'.
--    SQLite cannot ALTER a CHECK constraint, so rebuild the table — the same
--    dance migration 063 did for 'asset_profile'. FK from brand_slots(slot_kind)
--    references it by name and is preserved across the rename.
--
--    NOTE FOR ANYONE ADDING A SLOT KIND: stack.yaml says "append a row — no
--    schema or code change required elsewhere", and that is true for a kind
--    pointing at an EXISTING registry. A NEW registry is not that case: this
--    CHECK is what makes it a migration.
PRAGMA foreign_keys=OFF;

CREATE TABLE brand_slot_kinds_new (
    kind          TEXT PRIMARY KEY,
    registry      TEXT NOT NULL
                  CHECK (registry IN ('design_profile','stack_profile','principle_set','asset_profile','voice_profile')),
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

-- 2. Voice profiles — the scalars that are genuinely one value each.
--
--    Every column has a v0 counterpart, so migrating a profile is a move
--    rather than an invention:
--      tone        <- language.tone
--      locale      <- language.locale
--      casing      <- language.casing
--      sentences   <- language.voice.sentences
--      reference   <- language.voice.reference
--      owner       <- brand.owner
--      domain      <- brand.domain
CREATE TABLE IF NOT EXISTS voice_profiles (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    tone         TEXT,
    locale       TEXT NOT NULL DEFAULT 'en',
    casing       TEXT,
    sentences    TEXT,
    reference    TEXT,
    owner        TEXT,
    domain       TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','draft','archived')),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

-- 3. Everything else in a voice is a LIST OF RULES, so it is one table with a
--    kind discriminator rather than four tables or a JSON blob.
--
--    THE SHAPE WAS MEASURED, NOT ASSUMED. v0's `language` block carries five
--    list-ish parts and a first pass at this table modelled only one of them:
--
--      prefer         language.terminology.prefer   {word: the word it replaces}
--      avoid          language.terminology.avoid    [phrases]
--      banned_phrase  language.voice.banned_phrases [12 phrases]
--      formatting     language.formatting           {dates: "YYYY-MM-DD", …}
--      constraint     visual.constraints            [prose rules]
--
--    A (kind, key, value) triple holds all five: `formatting` and `prefer` use
--    both key and value, the three list kinds use key alone. A sixth kind
--    needs a row, not a migration — which is the same property brand_slot_kinds
--    has, and the reason this is the right shape rather than the compact one.
CREATE TABLE IF NOT EXISTS voice_profile_rules (
    profile_id  TEXT NOT NULL REFERENCES voice_profiles(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    note        TEXT,
    sort_order  INTEGER DEFAULT 0,
    PRIMARY KEY (profile_id, kind, key)
);

CREATE INDEX IF NOT EXISTS idx_voice_profile_rules_profile
    ON voice_profile_rules(profile_id, kind);

-- 4. Data seed for EXISTING installs ----------------------------------------
-- seed_registry() only runs on a fresh (empty) DB, so a stack.yaml addition
-- does NOT reach an already-seeded DB. Migrations are the upgrade path. All
-- idempotent; on a fresh install seed_registry() later writes the same values
-- from YAML.

INSERT OR REPLACE INTO brand_slot_kinds
    (kind, registry, required, cardinality, scope_filter, sort_order, description)
VALUES
    ('voice', 'voice_profile', 0, 'single', NULL, 60,
     'Voice — how the brand talks: tone, casing, locale, terminology, copy constraints.');
