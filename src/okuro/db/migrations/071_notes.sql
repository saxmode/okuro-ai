-- <!-- AGENT_HEADER
-- role: code
-- purpose: 071_notes — persistence for okuro-notes, the Obsidian-inspired note
--   surface. Markdown FORMAT, DB STORAGE (no files). Every note is chunked into
--   note_chunks and embedded into vec_note_chunks so notes are first-class RAG
--   citizens (cortex hybrid search + agent note_search). Wikilinks resolve into
--   note_links, which power backlinks + the note graph + agent traversal.
-- index: content
-- AGENT_HEADER_END -->
--
-- Storage shape mirrors slides (067) for the live-sync change-feed and follows
-- the thoughts/memory embed pattern: the vec_* table is NOT created here — the
-- migration runner calls ensure_vec_dims(empty_only=True) after migrations, and
-- vec_note_chunks is declared in embed/repair.py SPECS (source: note_chunks).
--
-- Chunk-level embedding (not whole-note) so long notes stay searchable, matching
-- cortex rather than the thoughts/memory whole-blob pattern. note_chunks is the
-- embed source of truth: re-chunked on every save, vectors rebuilt from it.

CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,                       -- uuid4
    title       TEXT NOT NULL DEFAULT 'Untitled',
    body        TEXT NOT NULL DEFAULT '',               -- markdown source
    frontmatter TEXT NOT NULL DEFAULT '{}',             -- JSON: typed properties
    project     TEXT,                                   -- project slug (nullable)
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_updated  ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_project  ON notes(project);
CREATE INDEX IF NOT EXISTS idx_notes_archived ON notes(archived);

-- Resolved + unresolved wikilinks. target_id is NULL while a [[link]] points at
-- a note/entity that does not exist yet (Obsidian-style "ghost" links); resolved
-- lazily on next save once the target appears.
CREATE TABLE IF NOT EXISTS note_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_note_id TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (target_type IN ('note','person','project','thought','artifact','kg','unresolved')),
    target_id   TEXT,                                   -- NULL when unresolved
    link_text   TEXT NOT NULL,                          -- the raw [[...]] payload
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_note_links_src    ON note_links(src_note_id);
CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target_type, target_id);

-- Embed source of truth. One row per chunk; rebuilt wholesale on every note save.
-- vec_note_chunks (vec0) is created/backfilled from this by ensure_vec_dims().
CREATE TABLE IF NOT EXISTS note_chunks (
    id        TEXT PRIMARY KEY,                         -- uuid4 per chunk
    note_id   TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    text      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_chunks_note ON note_chunks(note_id);

-- Excalidraw scenes. Can be standalone or embedded in a note via ![[drawing:id]].
-- scene is the full Excalidraw JSON; png_blob is a flattened raster for preview
-- and future OCR/caption → RAG.
CREATE TABLE IF NOT EXISTS drawings (
    id         TEXT PRIMARY KEY,                        -- uuid4
    note_id    TEXT,                                    -- nullable (standalone)
    title      TEXT NOT NULL DEFAULT 'Untitled drawing',
    scene      TEXT NOT NULL DEFAULT '{}',              -- JSON: Excalidraw scene
    png_blob   BLOB,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_drawings_note ON drawings(note_id);

-- Append-only change-feed for cross-process live sync (mirrors slides_events).
-- Web edits and stdio MCP edits both append; the SSE endpoint tails by seq so an
-- open note live-updates when any process mutates it. origin carries the writer's
-- client id so a client can ignore the echo of its own write.
CREATE TABLE IF NOT EXISTS notes_events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('saved', 'deleted')),
    origin  TEXT NOT NULL DEFAULT '',
    ts      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_events_seq ON notes_events(seq);
