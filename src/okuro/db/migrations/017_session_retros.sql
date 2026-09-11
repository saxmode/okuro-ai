-- <!-- AGENT_HEADER
-- role: code
-- purpose: 017_session_retros module
-- index: content
-- AGENT_HEADER_END -->
-- okuro session retros — causal post-mortem findings over low-score sessions.
--
-- Backs Meta-Harness P5 (causal reasoning over prior failures): a weekly
-- batch job compares recent low-score sessions against high-score ones and
-- extracts behavioral patterns that correlate with low compliance. Each
-- batch produces one row per processed session + optionally a memory if
-- the finding is well-supported (≥2 low sessions / ≤1 high session).

CREATE TABLE IF NOT EXISTS session_retros (
    session_id    TEXT PRIMARY KEY,                        -- matches sessions.session_id
    batch_id      TEXT NOT NULL,                           -- UUID shared across sessions analyzed in same run
    retro_at      TEXT DEFAULT (datetime('now')),
    score         INTEGER,                                 -- compliance_score at retro time
    cohort        TEXT NOT NULL CHECK (cohort IN ('low','high')),
    findings      TEXT,                                    -- JSON patterns for this session
    memory_id     TEXT,                                    -- gotcha memory written, if any (NULL if no pattern crossed threshold)
    model         TEXT                                     -- LLM that ran the analysis
);

CREATE INDEX IF NOT EXISTS idx_session_retros_batch ON session_retros(batch_id);
CREATE INDEX IF NOT EXISTS idx_session_retros_at    ON session_retros(retro_at DESC);

CREATE TABLE IF NOT EXISTS session_retro_batches (
    batch_id      TEXT PRIMARY KEY,
    ran_at        TEXT DEFAULT (datetime('now')),
    window_days   INTEGER,
    low_count     INTEGER,
    high_count    INTEGER,
    patterns_json TEXT,                                    -- top-level summary of patterns discovered
    memory_ids    TEXT,                                    -- JSON array of memories written this batch
    model         TEXT,
    error         TEXT
);
