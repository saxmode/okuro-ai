-- <!-- AGENT_HEADER
-- role: code
-- purpose: 028_people_graph — translation log, profile provenance, outbound questionnaires.
-- index: content
-- AGENT_HEADER_END -->
--
-- Supports the /people graph view:
--   * translation_log — every user→person rewrite (edge stats, audit trail)
--   * person_sources  — provenance per profile field (which file/questionnaire/preset wrote it)
--   * person_questionnaires — magic-link self-report forms sent to the recipient
--
-- Role presets live in roles/catalog/*.yaml under a `person_preset:` key
-- (read at request time by okuro.peer.presets). No cache table — YAML is
-- the source of truth and loader is fast.

CREATE TABLE IF NOT EXISTS translation_log (
    id              TEXT PRIMARY KEY,
    person_id       TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_text     TEXT NOT NULL,
    translated_text TEXT,
    context         TEXT,
    capability      TEXT DEFAULT 'translate',
    provider        TEXT,
    model           TEXT,
    duration_ms     INTEGER,
    success         INTEGER DEFAULT 1,
    error           TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_translation_log_person
    ON translation_log(person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_translation_log_success
    ON translation_log(success, created_at DESC);

-- Provenance — which source produced which profile field.
-- field_path examples: "communication.style", "cognitive.attention_span".
CREATE TABLE IF NOT EXISTS person_sources (
    id               TEXT PRIMARY KEY,
    person_id        TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_type      TEXT NOT NULL
                     CHECK (source_type IN ('preset','manual','eml','notes','pdf','questionnaire','inferred')),
    source_ref       TEXT,
    field_path       TEXT NOT NULL,
    extracted_value  TEXT NOT NULL,
    confidence       REAL DEFAULT 0.7,
    applied          INTEGER DEFAULT 1 CHECK (applied IN (0,1)),
    superseded_by    TEXT REFERENCES person_sources(id),
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_person_sources_person
    ON person_sources(person_id, applied, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_person_sources_type
    ON person_sources(source_type);

-- Questionnaires — outbound self-report forms with signed magic-link tokens.
-- `token` is URL-safe and unique; revocation = set status='expired'.
CREATE TABLE IF NOT EXISTS person_questionnaires (
    id           TEXT PRIMARY KEY,
    person_id    TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    token        TEXT NOT NULL UNIQUE,
    preset_key   TEXT,
    questions    TEXT NOT NULL DEFAULT '[]',
    responses    TEXT,
    status       TEXT DEFAULT 'sent'
                 CHECK (status IN ('sent','answered','applied','expired','revoked')),
    sent_at      TEXT DEFAULT (datetime('now')),
    answered_at  TEXT,
    applied_at   TEXT,
    expires_at   TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_person_questionnaires_person
    ON person_questionnaires(person_id, status);
CREATE INDEX IF NOT EXISTS idx_person_questionnaires_token
    ON person_questionnaires(token);
