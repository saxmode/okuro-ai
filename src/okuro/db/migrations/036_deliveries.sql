-- <!-- AGENT_HEADER
-- role: code
-- purpose: 036_deliveries — Stream C of the role-handover rewrite.
--   Audience-adapted renders of Stream B artifacts. One row per
--   (artifact_id, person_id, channel) emission. Mirrors translation_log
--   shape (typed cols + JSON payloads + duration_ms + provider/model).
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a new table, not a kind on artifacts:
--   The artifacts CHECK constraint (027) deliberately excludes "deliverable".
--   Audience-adapted renders are NOT artifacts — they are renders OF an
--   artifact for a specific recipient + channel + brand. Distinct lifecycle
--   (per-person, channel-typed payload, replayable), distinct consumer
--   (recipient via send-channel, not the okuro web UI), distinct
--   supersession (recipient receives the latest, never edits prior).
--
-- Why no FK on brand_id:
--   brands.id is FK-clean today, but the dispatcher routes brand_id
--   through stack_brand_resolve which can land on a profile-derived id
--   that isn't always present in the brands table. Treat as opaque text;
--   validation lives at stack.validator.lint_brand (already wired).
--
-- voice_preset on brands:
--   New column for audio channels (TTS, podcast). JSON {provider,
--   voice_id, style}. Used by P2/P3 channels; P1 reads but does not
--   require it.

PRAGMA foreign_keys = OFF;

-- ---------------------------------------------------------------------------
-- 1. deliveries — Stream C primary store
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS deliveries (
    id              TEXT PRIMARY KEY,
    artifact_id     TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    person_id       TEXT REFERENCES persons(id) ON DELETE SET NULL,
    -- No Microsoft proprietary formats. PDF (via Marp), HTML (microsite),
    -- markdown, audio (tts/podcast). PPTX/DOCX/XLSX are explicitly banned —
    -- open formats only. See convention memory 50b49e1c.
    channel         TEXT NOT NULL CHECK (channel IN
                      ('markdown', 'marp', 'microsite', 'tts', 'podcast')),
    brand_id        TEXT,
    title           TEXT,
    outline         TEXT NOT NULL DEFAULT '{}',
    theme           TEXT NOT NULL DEFAULT '{}',
    body            TEXT,
    body_blob       BLOB,
    body_path       TEXT,
    media_type      TEXT,
    duration_ms     INTEGER,
    cost_usd        REAL,
    provider        TEXT,
    model           TEXT,
    success         INTEGER NOT NULL DEFAULT 1,
    error           TEXT,
    created_by      TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_deliveries_artifact ON deliveries(artifact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_person   ON deliveries(person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_channel  ON deliveries(channel, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_brand    ON deliveries(brand_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_success  ON deliveries(success, created_at DESC);

CREATE TRIGGER IF NOT EXISTS deliveries_reject_empty_id_ins
BEFORE INSERT ON deliveries
FOR EACH ROW
WHEN NEW.id IS NULL OR TRIM(NEW.id) = ''
BEGIN
    SELECT RAISE(ABORT, 'deliveries.id must be a non-empty string');
END;

-- ---------------------------------------------------------------------------
-- 2. vec_deliveries — semantic recall over delivery bodies
-- ---------------------------------------------------------------------------

-- vec_deliveries created post-migrate by ensure_vec_dims() at active tier dim.

-- ---------------------------------------------------------------------------
-- 3. brands.voice_preset — audio channel routing
-- ---------------------------------------------------------------------------

ALTER TABLE brands ADD COLUMN voice_preset TEXT;  -- JSON {provider, voice_id, style}

PRAGMA foreign_keys = ON;
