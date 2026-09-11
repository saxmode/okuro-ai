-- <!-- AGENT_HEADER
-- role: code
-- purpose: okuro.stack — tech stack registry schema.
-- index: content
-- AGENT_HEADER_END -->
-- Tech stack registry: layers (taxonomy), entries (concrete choices),
-- dependency graph, alternatives, profiles (opinionated compositions),
-- and project bindings. Mirrors okuro.design shape: taxonomy + entries +
-- profiles + bindings, extended with a dependency graph and a lifecycle
-- status (trial/approved/deprecated/banned).

CREATE TABLE IF NOT EXISTS stack_layers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    category     TEXT NOT NULL,
    cardinality  TEXT NOT NULL DEFAULT 'single'
                 CHECK (cardinality IN ('single','multi')),
    sort_order   INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stack_entries (
    id             TEXT PRIMARY KEY,
    layer          TEXT NOT NULL REFERENCES stack_layers(id),
    name           TEXT NOT NULL,
    version        TEXT,
    status         TEXT NOT NULL DEFAULT 'trial'
                   CHECK (status IN ('trial','approved','deprecated','banned')),
    rationale      TEXT,
    use_when       TEXT DEFAULT '[]',
    avoid_when     TEXT DEFAULT '[]',
    docs_url       TEXT,
    owner          TEXT,
    replaces       TEXT REFERENCES stack_entries(id),
    last_reviewed  TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_stack_entries_layer  ON stack_entries(layer);
CREATE INDEX IF NOT EXISTS idx_stack_entries_status ON stack_entries(status);

CREATE TABLE IF NOT EXISTS stack_entry_deps (
    entry_id    TEXT NOT NULL REFERENCES stack_entries(id) ON DELETE CASCADE,
    depends_on  TEXT NOT NULL REFERENCES stack_entries(id),
    PRIMARY KEY (entry_id, depends_on)
);

CREATE TABLE IF NOT EXISTS stack_entry_alternatives (
    entry_id         TEXT NOT NULL REFERENCES stack_entries(id) ON DELETE CASCADE,
    alternative_id   TEXT NOT NULL,
    alternative_name TEXT,
    reason_rejected  TEXT,
    PRIMARY KEY (entry_id, alternative_id)
);

CREATE TABLE IF NOT EXISTS stack_profiles (
    name         TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','draft','archived')),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stack_profile_entries (
    profile_name  TEXT NOT NULL REFERENCES stack_profiles(name) ON DELETE CASCADE,
    entry_id      TEXT NOT NULL REFERENCES stack_entries(id),
    role          TEXT NOT NULL DEFAULT 'primary'
                  CHECK (role IN ('primary','secondary')),
    PRIMARY KEY (profile_name, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_stack_profile_entries_entry
    ON stack_profile_entries(entry_id);

CREATE TABLE IF NOT EXISTS stack_project_profile (
    project_slug  TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    profile_name  TEXT NOT NULL REFERENCES stack_profiles(name),
    assigned_at   TEXT DEFAULT (datetime('now'))
);

-- Proposals: audit log for lifecycle transitions and new-entry requests.
CREATE TABLE IF NOT EXISTS stack_proposals (
    id             TEXT PRIMARY KEY,
    entry_id       TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'new'
                   CHECK (kind IN ('new','promote','deprecate','ban','reinstate')),
    proposed_by    TEXT,
    rationale      TEXT,
    payload        TEXT DEFAULT '{}',
    outcome        TEXT NOT NULL DEFAULT 'pending'
                   CHECK (outcome IN ('pending','accepted','rejected')),
    decided_by     TEXT,
    decided_at     TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_stack_proposals_outcome
    ON stack_proposals(outcome);
