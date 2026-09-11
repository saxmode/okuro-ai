-- <!-- AGENT_HEADER
-- role: code
-- purpose: 031_kg — temporal knowledge graph (mempalace pattern, valid_from/valid_to triples).
-- index: content
-- AGENT_HEADER_END -->
--
-- Temporal subject-predicate-object store with validity windows. Distinguishes
-- "X was true Q1, Y is true Q2 onward" from flat replacement (which agent_memory
-- already does via supersedes). Pairs with future contradiction-detection hook.
--
-- Triples reference back to the source memory or artifact that asserted them
-- so an invalidation cascade can follow provenance. NULL valid_to = still true.

CREATE TABLE IF NOT EXISTS kg_entities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'concept',
    properties      TEXT NOT NULL DEFAULT '{}',
    project         TEXT REFERENCES projects(id),
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_name    ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type    ON kg_entities(type);
CREATE INDEX IF NOT EXISTS idx_kg_entities_project ON kg_entities(project);

CREATE TABLE IF NOT EXISTS kg_triples (
    id                  TEXT PRIMARY KEY,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object              TEXT NOT NULL,
    valid_from          TEXT,
    valid_to            TEXT,
    project             TEXT REFERENCES projects(id),
    source_memory_id    TEXT REFERENCES agent_memory(id),
    source_artifact_id  TEXT REFERENCES artifacts(id),
    confidence          REAL NOT NULL DEFAULT 0.7,
    created_at          TEXT DEFAULT (datetime('now')),
    invalidated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_kg_triples_subject   ON kg_triples(subject);
CREATE INDEX IF NOT EXISTS idx_kg_triples_predicate ON kg_triples(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_triples_object    ON kg_triples(object);
CREATE INDEX IF NOT EXISTS idx_kg_triples_validity  ON kg_triples(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_kg_triples_project   ON kg_triples(project);
