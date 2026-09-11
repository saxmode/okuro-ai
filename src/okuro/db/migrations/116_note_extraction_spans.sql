-- Migration 116 — store each extracted item's verified source span.
--
-- item_key (extractor.item_key, 2026-07-25) hashes the NOTE's words rather
-- than the model's, so a re-extraction of an unchanged note keys stably. It
-- keys on EXACT span equality, which leaves one leak: the model may quote the
-- same sentence with different boundaries on a later run ("meridian.example.com
-- fails to communicate what Meridian can do for users" vs "...what Meridian is
-- capable of" — one sentence, two quotes, two keys, two signals). Measured
-- 2026-07-26: 123 of 236 notes had been extracted on 2+ separate days, and
-- the notes queue held 986 open signals from 236 notes.
--
-- Suppressing that requires COMPARING spans across runs, which requires
-- keeping them. The hash cannot be reversed, so the normalised span is stored
-- alongside it.
--
-- span_norm is the _normalize()d span, stored ONLY when it was verified
-- against the note body and long enough to identify something — the same test
-- item_key applies before trusting it. NULL means "this item is keyed on the
-- model's wording", and no span comparison is valid for it.
--
-- Additive and nullable: rows written before this migration simply have NULL,
-- and an older build ignores the column. Existing rows are NOT backfilled —
-- the span was never persisted, so there is nothing to backfill from. They
-- converge naturally as their notes are re-extracted.
--
-- Rollback (SQLite forward-only; the column is additive and nullable):
--   -- ALTER TABLE note_extractions DROP COLUMN span_norm;

ALTER TABLE note_extractions ADD COLUMN span_norm TEXT;

-- The lookup is always "every stored span for THIS note" — idx_note_extractions_note
-- already covers note_id; this partial index keeps the span scan off the rows
-- that have none.
CREATE INDEX IF NOT EXISTS idx_note_extractions_span
    ON note_extractions(note_id, span_norm)
    WHERE span_norm IS NOT NULL;
