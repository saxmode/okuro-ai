-- <!-- AGENT_HEADER
-- role: code
-- purpose: 029_memory_pointers — AAAK-style compact pointer index over agent_memory.
-- index: content
-- AGENT_HEADER_END -->
--
-- mempalace-inspired closet/AAAK index layer. Each row is a one-line pointer
-- into agent_memory, structured so bootstrap can surface 30 pointer lines for
-- the same token cost as ~10 full memory bodies. Pointers are extracted
-- heuristically at write_memory() time (no LLM dependency — okuro must work
-- without local inference per architecture decision).
--
-- Format the renderer emits:
--   [topic] short-title | entities | flags | →memory_id_prefix
--
-- ON DELETE CASCADE: when a memory is hard-deleted, its pointer goes too.

CREATE TABLE IF NOT EXISTS memory_pointers (
    memory_id     TEXT PRIMARY KEY REFERENCES agent_memory(id) ON DELETE CASCADE,
    topic_label   TEXT NOT NULL DEFAULT '',
    entities      TEXT NOT NULL DEFAULT '',
    flags         TEXT NOT NULL DEFAULT '',
    project       TEXT,
    topic_kind    TEXT NOT NULL,
    confidence    REAL DEFAULT 0.7,
    generated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_pointers_project   ON memory_pointers(project);
CREATE INDEX IF NOT EXISTS idx_memory_pointers_topic     ON memory_pointers(topic_kind);
CREATE INDEX IF NOT EXISTS idx_memory_pointers_conf      ON memory_pointers(confidence);
