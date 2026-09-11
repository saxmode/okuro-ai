-- <!-- AGENT_HEADER
-- role: code
-- purpose: person_sources.source_type → free TEXT (drop CHECK) so web/URL-sourced
--   claims can be recorded. Validated at the writer (okuro.peer.sources
--   _VALID_SOURCE_TYPES), mirroring migration 056's "document the set in code"
--   choice — adding a source type is now a one-line code change, not a rebuild.
-- index: content
-- AGENT_HEADER_END -->
-- The audience-research flow (deck-for-the-board-of-X) enriches people from the
-- web: each claim ("understands numbers", conf 0.8, source xy.com) lands as a
-- person_sources row. The original CHECK enum (preset/manual/eml/notes/pdf/
-- questionnaire/inferred) has no 'web'/'url' value, and SQLite cannot ALTER a
-- CHECK — so rebuild the table once, without the CHECK, and never again.
--
-- superseded_by is a self-FK: the _new table references person_sources(id) by
-- the FINAL name, resolved after the rename (foreign_keys OFF during rebuild).
PRAGMA foreign_keys=OFF;

CREATE TABLE person_sources_new (
    id               TEXT PRIMARY KEY,
    person_id        TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_type      TEXT NOT NULL,                       -- validated at writer, not DB
    source_ref       TEXT,                                -- filename | preset id | URL
    field_path       TEXT NOT NULL,
    extracted_value  TEXT NOT NULL,
    confidence       REAL DEFAULT 0.7,
    applied          INTEGER DEFAULT 1 CHECK (applied IN (0,1)),
    superseded_by    TEXT REFERENCES person_sources(id),
    created_at       TEXT DEFAULT (datetime('now'))
);

INSERT INTO person_sources_new
    (id, person_id, source_type, source_ref, field_path,
     extracted_value, confidence, applied, superseded_by, created_at)
SELECT id, person_id, source_type, source_ref, field_path,
       extracted_value, confidence, applied, superseded_by, created_at
FROM person_sources;

DROP TABLE person_sources;
ALTER TABLE person_sources_new RENAME TO person_sources;

CREATE INDEX IF NOT EXISTS idx_person_sources_person
    ON person_sources(person_id, applied, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_person_sources_type
    ON person_sources(source_type);

PRAGMA foreign_keys=ON;
