-- Migration 117 — give authorship a home in the notes schema.
--
-- Every timestamp on a note is an INGESTION timestamp. `created_at` is when
-- okuro first saw the note; for an import it is the import instant (or the
-- source file's mtime, which _apply_source_mtime backdates it to). Nothing
-- records when the human actually wrote the thing.
--
-- That gap is what put April-authored notes at the top of the inbox: migrated
-- 2026-07-25, ranked as one day old, holding 8 of the 10 note+signal shelf
-- slots. The stop-gap (2026-07-26) reads `frontmatter.original_date` — real
-- data, but JSON in the ranking hot path, untyped, and unreachable by any
-- query that wants to sort or filter on authorship.
--
-- authored_at is that value as a first-class column: "when the human wrote
-- it", NULL when unknown.
--
-- NULL IS THE HONEST ANSWER FOR MOST ROWS, and this migration deliberately
-- does not guess:
--
--   * 8 notes carry frontmatter.original_date from an agent migration. Those
--     are backfilled below — a recorded authorship date, not an inference.
--   * 449 notes came from the Obsidian vault bulk import. Their authorship is
--     GONE: 295 of 413 vault files share mtime 2026-03-19 (the vault was
--     copied), and — measured 2026-07-27 — exactly 0 of 425 vault markdown
--     files carry a YAML `date`/`created` key to recover it from. Only one
--     file has YAML frontmatter at all, and its single key is `a`. There is
--     nothing to read, so these stay NULL rather than inheriting a mtime that
--     would assert an authorship date we know to be false.
--
-- Consumers resolve COALESCE(authored_at, frontmatter.original_date,
-- created_at) via sense.inbox.reducer._note_anchors — one resolution, so a
-- todo and a signal lifted from the same note can never rank on different
-- clocks. The frontmatter arm stays as a fallback for rows an agent writes
-- that way; new callers should pass authored_at to upsert_note instead.
--
-- Rollback (SQLite forward-only; the column is additive and nullable, and an
-- older build simply ignores it):
--   -- ALTER TABLE notes DROP COLUMN authored_at;

ALTER TABLE notes ADD COLUMN authored_at TEXT;

-- Backfill the only rows where authorship was ever recorded. Guarded exactly
-- as the resolver guards it: the value must look like a date, and must not
-- postdate ingestion (authorship after import is impossible, so a later date
-- is a bug or a lie and created_at wins).
UPDATE notes
   SET authored_at = substr(json_extract(frontmatter, '$.original_date'), 1, 10)
 WHERE json_valid(frontmatter)
   AND json_extract(frontmatter, '$.original_date') IS NOT NULL
   AND substr(json_extract(frontmatter, '$.original_date'), 1, 10)
       GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
   AND substr(json_extract(frontmatter, '$.original_date'), 1, 10)
       <= substr(created_at, 1, 10);

-- Ranking reads it per note id (see _note_anchors); sorting the notes list by
-- authorship is the other intended use. Partial: most rows are NULL by design.
CREATE INDEX IF NOT EXISTS idx_notes_authored_at
    ON notes(authored_at)
    WHERE authored_at IS NOT NULL;
