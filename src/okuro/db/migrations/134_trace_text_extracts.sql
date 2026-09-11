-- <!-- AGENT_HEADER
-- role: code
-- purpose: 134_trace_text_extracts — persist everything derived from agent_events raw text before compaction can null it.
-- index: content
-- AGENT_HEADER_END -->
--
-- The class of defect this closes: DERIVED DATA WHOSE SOURCE IS DESTROYABLE BY
-- ANOTHER PROCESS.
--
-- Two rebuild paths read agent_events raw text with an unwindowed LIKE scan:
--   bridge.py    text LIKE '%Session ID:%'    -> session_bridge  (623 links,
--                the ONLY recovery path back to 2026-04-12)
--   improve.py   text LIKE '%Adopt this role%' -> session_roles  (381 links)
-- Both writers are additive upserts, so the hourly daemon run is harmless. But
-- `bridge_sessions(rebuild=True)` and `index_session_roles(rescan=True)` DELETE
-- their table first and rebuild it from the raw text — and lifecycle.compact_traces
-- nulls exactly that text at the cool tier (tool_result, 90-180d), which is where
-- the bootstrap packet lives. A routine "re-run the extraction after changing the
-- regex" therefore becomes one-way data loss the moment compaction has run once.
--
-- The fix is not a guard on rebuild. It is to stop treating raw text as the
-- durable home of a derived link: harvest first, into these tables, and point the
-- rebuild paths here. Raw text then becomes a cache of its own extract, and
-- nulling it costs nothing that was not already persisted.
--
-- Storage note: extracts are tiny (a uuid or a role slug per hit), so this is not
-- a second copy of the corpus. Only the extracted VALUE is kept, never the span
-- it came from.

-- ---------------------------------------------------------------------------
-- 1. The harvested extracts
-- ---------------------------------------------------------------------------
-- One row per (extractor, session, event ordinal, extracted value). `ord` keeps
-- the position so a later consumer can still say "the first hit in the session
-- wins" without re-reading the transcript.
--
-- APPEND-ONLY BY CONTRACT. Nothing in okuro may DELETE from this table: it is
-- the durable side of data whose raw source is scheduled for destruction, and a
-- delete here is exactly the loss the table exists to prevent. Pinned by
-- tests/sense/test_trace_extract_registry.py.
CREATE TABLE IF NOT EXISTS trace_text_extracts (
    extractor         TEXT NOT NULL,     -- registry name, e.g. 'session_id_link'
    native_session_id TEXT NOT NULL,     -- agent_sessions.session_id
    ord               INTEGER NOT NULL DEFAULT -1,  -- agent_events.ord of the hit
    value             TEXT NOT NULL,     -- the extracted token (uuid, role slug, ...)
    extra             TEXT NOT NULL DEFAULT '{}',   -- JSON: per-extractor detail
    harvested_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (extractor, native_session_id, ord, value)
);

CREATE INDEX IF NOT EXISTS idx_trace_text_extracts_session
    ON trace_text_extracts(native_session_id);
CREATE INDEX IF NOT EXISTS idx_trace_text_extracts_value
    ON trace_text_extracts(extractor, value);

-- ---------------------------------------------------------------------------
-- 2. The harvest watermark
-- ---------------------------------------------------------------------------
-- Same shape and same reason as `interaction_scanned` (migration 113): hits are
-- sparse, so their ABSENCE cannot distinguish "this session had no Session ID
-- line" from "no extractor has looked at this session yet". Compaction reads
-- this table to decide whether nulling a session's bodies is safe, and it fails
-- closed — a session with no row here is never compacted.
--
-- `hits` is recorded because it is the only honest signal about damage already
-- done: a session whose tool_result bodies were nulled by an earlier compaction
-- run harvests 0 hits and is indistinguishable, from here on, from a session
-- that genuinely never carried the string. The shortfall shows up as a corpus
-- total below the 623/381 baselines, not as a per-session flag.
CREATE TABLE IF NOT EXISTS trace_extract_scanned (
    extractor         TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    hits              INTEGER NOT NULL DEFAULT 0,
    scanned_at        TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (extractor, native_session_id)
);

CREATE INDEX IF NOT EXISTS idx_trace_extract_scanned_session
    ON trace_extract_scanned(native_session_id);
