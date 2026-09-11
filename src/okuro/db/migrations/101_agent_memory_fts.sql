-- Migration 101 — FTS5 BM25 sparse index over agent_memory (recall-ceiling fix).
--
-- Memory read was PURE vector (vec_memory). Measured recall@10 ceils at
-- ~0.68-0.72 even unfloored (docs/research/okuro-memory-recall-ceiling.md): a
-- third of the misses are real (not sibling absorption) and code-flavored —
-- file paths, symbols, error strings, config values the dense model
-- under-ranks but an exact-token match nails. Adding a BM25 lexical arm and
-- RRF-fusing it with the vector arm lifts eval recall@10 to ~0.98 with ZERO
-- junk-rejection regression (the fusion only fires when the floored vector arm
-- is non-empty, so off-domain junk — which has an empty vector arm — stays
-- rejected by the same 0.50 cosine floor as before).
--
-- Mirrors the cortex_fts pattern (migration 057): external-content FTS5 +
-- keep-in-sync triggers, so EVERY write path to agent_memory (write_memory,
-- supersede confidence drop, utility decay UPDATE, hygiene) stays indexed
-- without touching each call site. FTS5 is already compiled into this SQLite
-- build (cortex_fts, agent_events_fts use it) — zero new dependencies.
--
-- agent_memory has a TEXT primary key but a normal integer rowid (NOT WITHOUT
-- ROWID), so content_rowid='rowid' works; the search joins
-- agent_memory_fts.rowid = agent_memory.rowid to recover the id.
--
-- Indexed columns: topic + content (the same content column fed to the
-- embedder, plus the topic tag so 'gotcha'/'convention'/etc. are matchable).
-- Superseded rows stay in FTS; the read-path lexical query filters them out
-- structurally, exactly as the vector path does (_EXCLUDE_SUPERSEDED).
--
-- Idempotent + install-portable: IF NOT EXISTS guards + a one-time 'rebuild'
-- backfill from existing agent_memory. Re-running is prevented by _migrations
-- and 'rebuild' is itself idempotent. No machine paths.

CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
    topic,
    content,
    content='agent_memory',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

-- Keep-in-sync triggers (external-content contentless-delete idiom).
CREATE TRIGGER IF NOT EXISTS agent_memory_fts_ai AFTER INSERT ON agent_memory
BEGIN
    INSERT INTO agent_memory_fts(rowid, topic, content)
    VALUES (new.rowid, new.topic, new.content);
END;

CREATE TRIGGER IF NOT EXISTS agent_memory_fts_ad AFTER DELETE ON agent_memory
BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, topic, content)
    VALUES ('delete', old.rowid, old.topic, old.content);
END;

CREATE TRIGGER IF NOT EXISTS agent_memory_fts_au AFTER UPDATE ON agent_memory
BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, topic, content)
    VALUES ('delete', old.rowid, old.topic, old.content);
    INSERT INTO agent_memory_fts(rowid, topic, content)
    VALUES (new.rowid, new.topic, new.content);
END;

-- One-time backfill from existing agent_memory. On a fresh install
-- agent_memory is empty so this is a no-op.
INSERT INTO agent_memory_fts(agent_memory_fts) VALUES ('rebuild');
