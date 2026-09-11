-- <!-- AGENT_HEADER
-- role: code
-- purpose: Durable resonance interview_session — makes the enrich⇄analyze loop
--   resumable + UI-backable. Was ephemeral: the caller threaded the growing
--   artifact-id list + goal through each turn. Now a stored session owns the
--   goal, the evidence set, the last-computed completeness/questions, and a turn
--   log — so an interview can be paused, listed, and resumed.
-- index: content
-- AGENT_HEADER_END -->
--
-- One row per prepare-content interview. The evidence artifact ids (Q&A turns,
-- ingested docs, research findings) are the session state the gap engine reads;
-- completeness/ready/open_questions cache the last analyze so a resumed session
-- renders without re-running the LLM. The turn log is for resume + UI replay.

CREATE TABLE IF NOT EXISTS resonance_sessions (
    id             TEXT PRIMARY KEY,                    -- slug/uuid
    goal           TEXT NOT NULL,
    project        TEXT,
    person_id      TEXT,                                -- optional recipient/audience
    brand_id       TEXT,
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','ready','closed')),
    artifact_ids   TEXT NOT NULL DEFAULT '[]',          -- JSON array of evidence ids
    completeness   REAL,                                -- last analyze (0.0-1.0)
    ready          INTEGER NOT NULL DEFAULT 0,
    open_questions TEXT NOT NULL DEFAULT '[]',          -- JSON array of last questions
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_resonance_sessions_status  ON resonance_sessions(status);
CREATE INDEX IF NOT EXISTS idx_resonance_sessions_project ON resonance_sessions(project);

-- Turn log — one row per evidence-adding event. Every turn is also an evidence
-- artifact (in artifact_ids); this table keeps the human-readable Q/A + kind for
-- resume + UI replay without re-reading each artifact body.
CREATE TABLE IF NOT EXISTS resonance_session_turns (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES resonance_sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'answer'
                CHECK (kind IN ('answer','doc','research')),
    question    TEXT,
    answer      TEXT,
    artifact_id TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_resonance_session_turns ON resonance_session_turns(session_id);
