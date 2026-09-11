-- <!-- AGENT_HEADER
-- role: code
-- purpose: Person CRM goes relational — companies + affiliations (hats) +
--   connections (user edges) + engagements (project × hat × company).
-- index: content
-- AGENT_HEADER_END -->
--
-- The flat `persons` table is single-org / single-role / one-global-profile.
-- A real contact (e.g. a CEO at one company who also sits on another's board,
-- is a stakeholder in a third, and is an end-user of its product) cannot be
-- expressed — multi-hat
-- reality leaks into the `tags` blob. This migration introduces the relational
-- shape so okuro can resolve, per project: information architecture from the
-- PERSON's cognitive profile + visual design from the COMPANY's brand.
--
-- `persons` is intentionally LEFT UNCHANGED here. Its `organization`/`role`
-- columns become a denormalised "primary hat" cache (kept for back-compat and
-- embedding text), backfilled per install by a local seeding script. The cognitive
-- per-axis provenance shape is a JSON change, no DDL needed.
--
-- ref_id-style FKs that point across registries (companies.brand_id → brands)
-- are real FKs because both tables live in this DB. Enum-like columns
-- (affiliations.role_class, connections.connection_type) are free TEXT,
-- validated at the writer — mirrors migration 023's stack_profiles.scope choice
-- so adding a new class is a code change, not a schema migration.

-- 1. Companies — the entity that owns a design identity --------------------
CREATE TABLE IF NOT EXISTS companies (
    id          TEXT PRIMARY KEY,                       -- slug, e.g. 'northwind'
    name        TEXT NOT NULL,
    brand_id    TEXT REFERENCES brands(id),             -- design profile slot; NULL until brands mature
    domain      TEXT,                                   -- primary web domain
    notes       TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_companies_brand ON companies(brand_id);

-- 2. Affiliations — person → company "hats" (multi per person) -------------
-- role_class maps to ROLE_DEFAULT_SLIDERS keys (ceo/cto/engineer/client/…) so
-- a hat can seed slider priors. is_primary marks the headline hat shown on the
-- People tab; enforced single-primary-per-person at the writer.
CREATE TABLE IF NOT EXISTS affiliations (
    id          TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    company_id  TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    role        TEXT,                                   -- "CEO", "Board member", "Stakeholder"
    role_class  TEXT,                                   -- ROLE_DEFAULT_SLIDERS key, validated at writer
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','past')),
    is_primary  INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    ended_at    TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_affiliations_person  ON affiliations(person_id);
CREATE INDEX IF NOT EXISTS idx_affiliations_company ON affiliations(company_id);
-- At most one primary hat per person (partial unique index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_affiliations_primary
    ON affiliations(person_id) WHERE is_primary = 1;

-- 3. Connections — person → USER edges (your relationship, per context) -----
-- connection_type is free TEXT (co-shareholder | customer | vendor | board-peer
-- | friend | family | …), validated at the writer. company_id optionally ties
-- the connection to the company it's about (e.g. co-shareholder @ a joint venture).
CREATE TABLE IF NOT EXISTS connections (
    id              TEXT PRIMARY KEY,
    person_id       TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    connection_type TEXT NOT NULL,
    context         TEXT,                               -- "the co-op", "home install"
    company_id      TEXT REFERENCES companies(id) ON DELETE SET NULL,
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_connections_person  ON connections(person_id);
CREATE INDEX IF NOT EXISTS idx_connections_company ON connections(company_id);

-- 4. Engagements — project × person-hat × company --------------------------
-- The runtime join that engagement_resolve() reads: which person, wearing
-- which hat (affiliation), under which company's design profile, on a project.
CREATE TABLE IF NOT EXISTS engagements (
    id              TEXT PRIMARY KEY,
    project_slug    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    person_id       TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    affiliation_id  TEXT REFERENCES affiliations(id) ON DELETE SET NULL,  -- which hat
    company_id      TEXT REFERENCES companies(id) ON DELETE SET NULL,     -- whose design profile
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_engagements_project ON engagements(project_slug);
CREATE INDEX IF NOT EXISTS idx_engagements_person  ON engagements(person_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_engagements_project_person
    ON engagements(project_slug, person_id);
