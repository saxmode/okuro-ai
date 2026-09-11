-- <!-- AGENT_HEADER
-- role: code
-- purpose: 072_note_images — raster image attachments for okuro-notes. Mirrors
--   the drawings (071) blob-in-DB pattern: bytes live in the DB, never on disk,
--   so a note stays self-contained (markdown FORMAT, DB STORAGE). Pasted/dropped
--   images upload here and are referenced from the body as
--   ![alt](/api/notes/images/<id>). png_blob → note_images so the whole vault is
--   one backup unit and future OCR/caption → RAG has a source.
-- index: content
-- AGENT_HEADER_END -->
--
-- No vec table here — image OCR/captioning is a later phase. The bytes are the
-- source of truth; note_id is nullable so an image can be uploaded before the
-- owning note has an id (parity with drawings.note_id).

CREATE TABLE IF NOT EXISTS note_images (
    id         TEXT PRIMARY KEY,                        -- uuid4
    note_id    TEXT,                                    -- nullable (upload-before-save)
    mime       TEXT NOT NULL DEFAULT 'image/png',
    filename   TEXT,                                    -- original name, best-effort
    bytes      BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_note_images_note ON note_images(note_id);
