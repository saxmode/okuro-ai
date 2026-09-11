-- Migration 057 — FTS5 BM25 sparse index over cortex_docs (audit F25).
--
-- The hybrid search's keyword arm was hand-rolled substring membership
-- (`token in purpose`) — no term frequency, no inverse document frequency, no
-- length normalization. A token that appears in 100k docs counted the same as
-- a rare distinctive token. FTS5 (already compiled into this SQLite build —
-- agent_events_fts uses it) gives real BM25: TF·IDF + length-norm, for zero
-- new dependencies.
--
-- SYNC STRATEGY — external-content FTS5 + triggers (NOT a hand-maintained
-- parallel table). Rationale:
--   * `content='cortex_docs'` means FTS5 stores only the inverted index, not a
--     second copy of the (large) document text — cortex_docs already holds it.
--   * Triggers fire on EVERY insert/delete to cortex_docs regardless of which
--     code path issues the DML (index_file, _remove_file, reclaim tombstone,
--     clear). A hand-maintained parallel table would have to be updated at
--     each of those sites and silently rots the day a new write path is added.
--     Triggers cannot be forgotten.
--   * cortex_docs has a stable integer rowid; FTS rows are keyed by it and the
--     search joins cortex_fts.rowid = cortex_docs.rowid to recover id/project.
-- Tombstones: a soft-deleted (deleted_at) row stays in FTS; the search join
-- already filters `deleted_at IS NULL`, so tombstoned docs never surface.
--
-- The indexed text is the same `document` column fed to the embedder, plus the
-- `purpose` and `section` so distinctive header/section terms are matchable.
--
-- Idempotent + install-portable: IF NOT EXISTS guards + a one-time backfill
-- from existing cortex_docs via the FTS 'rebuild' command. No machine paths.

CREATE VIRTUAL TABLE IF NOT EXISTS cortex_fts USING fts5(
    purpose,
    section,
    document,
    content='cortex_docs',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

-- Keep-in-sync triggers (external-content contentless-delete idiom).
CREATE TRIGGER IF NOT EXISTS cortex_fts_ai AFTER INSERT ON cortex_docs
BEGIN
    INSERT INTO cortex_fts(rowid, purpose, section, document)
    VALUES (new.rowid, new.purpose, new.section, new.document);
END;

CREATE TRIGGER IF NOT EXISTS cortex_fts_ad AFTER DELETE ON cortex_docs
BEGIN
    INSERT INTO cortex_fts(cortex_fts, rowid, purpose, section, document)
    VALUES ('delete', old.rowid, old.purpose, old.section, old.document);
END;

CREATE TRIGGER IF NOT EXISTS cortex_fts_au AFTER UPDATE ON cortex_docs
BEGIN
    INSERT INTO cortex_fts(cortex_fts, rowid, purpose, section, document)
    VALUES ('delete', old.rowid, old.purpose, old.section, old.document);
    INSERT INTO cortex_fts(rowid, purpose, section, document)
    VALUES (new.rowid, new.purpose, new.section, new.document);
END;

-- One-time backfill from existing cortex_docs. 'rebuild' reads the external
-- content table and repopulates the whole index; safe + idempotent (re-running
-- the migration is prevented by _migrations, and 'rebuild' is itself
-- idempotent). On a fresh install cortex_docs is empty so this is a no-op.
INSERT INTO cortex_fts(cortex_fts) VALUES ('rebuild');
