-- <!-- AGENT_HEADER
-- role: code
-- purpose: 094_notes_extract — make okuro-notes a first-class extraction source.
--   Adds 'notes' to signals.source, plus the per-note watermark and the
--   item ledger that keep the extractor idempotent across re-runs.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why 'notes' earns its own source rather than riding under 'proactive':
-- source is the indexed, first-class provenance field the Inbox filters and
-- counts on. Notes are a user-authored input surface, not a heuristic scan of
-- okuro's own state — blending the two would make "what did my notes surface
-- this week?" unanswerable without reaching into evidence JSON. The other
-- three sources each name an origin; so does this one.
--
-- The Obsidian bridge (sense/bridge/obsidian.py) is being deferred in favour
-- of native notes. Its 191 already-ingested thoughts keep their
-- metadata.source='obsidian' tag and are untouched by this migration —
-- knowledge is preserved, only the live intake moves.
--
-- Rollback (SQLite forward-only): re-run the rebuild below with the original
-- 4-value CHECK after `DELETE FROM signals WHERE source = 'notes';`, then
-- `DROP TABLE note_extractions; DROP TABLE note_extract_state;`.

-- ── 1. signals.source += 'notes' ────────────────────────────────────────────
-- SQLite cannot ALTER a CHECK in place; this is the documented 12-step
-- table-rebuild (https://sqlite.org/lang_altertable.html#otheralter), same
-- procedure 027 used to tighten artifacts.kind. foreign_keys=OFF is required
-- because todos.source_signal_id REFERENCES signals(id) — the FK re-resolves
-- to the rebuilt table after RENAME. signals carries no triggers and no views
-- (verified against the live DB), so indexes are the only dependents to
-- recreate.
--
-- The migration runner routes any script containing PRAGMA through raw
-- executescript (see SQLiteDB._self_manages_txn) — these PRAGMAs are honoured
-- rather than silently ignored inside an injected transaction.

PRAGMA foreign_keys = OFF;

CREATE TABLE signals_new (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL
                      CHECK (source IN ('sysmon','proactive','orchestrator','manual','notes')),
    source_ref        TEXT,
    severity          TEXT NOT NULL
                      CHECK (severity IN ('info','warn','crit')),
    summary           TEXT NOT NULL,
    evidence          TEXT NOT NULL DEFAULT '{}',
    suggested_action  TEXT,
    auto_promote      INTEGER NOT NULL DEFAULT 0
                      CHECK (auto_promote IN (0,1)),
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','promoted','discarded','ignored','expired')),
    discard_reason    TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at        TEXT
);

INSERT INTO signals_new
SELECT id, source, source_ref, severity, summary, evidence, suggested_action,
       auto_promote, status, discard_reason, created_at, expires_at
FROM signals;

DROP TABLE signals;
ALTER TABLE signals_new RENAME TO signals;

CREATE INDEX IF NOT EXISTS idx_signals_status_severity_created
    ON signals(status, severity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_source
    ON signals(source);

PRAGMA foreign_keys = ON;

-- ── 2. per-note watermark ───────────────────────────────────────────────────
-- The extractor is LLM-backed, so re-reading an unchanged note is pure cost
-- (DP01). content_hash gates the call: a note is only sent to the bridge when
-- its body actually changed since the last successful extraction. Notes
-- autosave aggressively, so this is the difference between ~20 calls/day and
-- one per cron tick per note.

CREATE TABLE IF NOT EXISTS note_extract_state (
    note_id       TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 0,
    extracted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 3. item ledger ──────────────────────────────────────────────────────────
-- One row per extracted item, keyed by a content-stable item_key so editing a
-- note's unrelated paragraph cannot resurrect an item the user already
-- actioned. Also the provenance join: given a todo, `routed_to`/`target_id`
-- answer "which note said this, and why did okuro list it?".
--
-- The tag block (topic / entities / context / rationale) is denormalised here
-- as well as written into the destination row, because destinations disagree
-- on where free-form metadata lives (todos.context JSON vs signals.evidence
-- JSON vs thoughts.metadata JSON). This table is the one place every item's
-- tags are queryable with the same shape.

CREATE TABLE IF NOT EXISTS note_extractions (
    id         TEXT PRIMARY KEY,
    note_id    TEXT NOT NULL,
    item_key   TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL
               CHECK (kind IN ('todo','signal','idea','question')),
    routed_to  TEXT NOT NULL
               CHECK (routed_to IN ('todos','signals','thoughts')),
    target_id  TEXT,
    text       TEXT NOT NULL,
    topic      TEXT,
    entities   TEXT NOT NULL DEFAULT '[]',
    context    TEXT,
    rationale  TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_note_extractions_note   ON note_extractions(note_id);
CREATE INDEX IF NOT EXISTS idx_note_extractions_target ON note_extractions(routed_to, target_id);
CREATE INDEX IF NOT EXISTS idx_note_extractions_topic  ON note_extractions(topic);
