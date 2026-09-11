-- <!-- AGENT_HEADER
-- role: code
-- purpose: 062_commitments — per-session LLM-inferred implicit follow-ups (OpenClaw-style auto-capture), surfaced via the inbox as kind='commitment'.
-- index: content
-- AGENT_HEADER_END -->
--
-- Phase 3a of the unified Inbox. Additive-only. The daemon "commitment-infer"
-- task scans recently-active provider sessions (agent_sessions), bundles each
-- session's agent_events, and asks an LLM (via bridge.invoke) to extract the
-- concrete implicit follow-ups the user/agent left undone or promised. Each
-- extracted item becomes one `commitments` row, which the inbox reducer then
-- projects as kind='commitment'.
--
-- TTL: commitments default to a 7-day expiry. The infer task folds an expiry
-- sweep (open + past-due -> 'expired') into the same run, mirroring the
-- review_queue precedent (no second daemon task).
--
-- `commitment_scans` is the watermark table: one row per processed
-- agent_session so the (expensive) LLM pass never re-runs on an already-scanned
-- session, even when that session emitted zero commitments or the provider
-- failed.

CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,                        -- "commit:{agent_session_id}:{slug}"
    session_id TEXT NOT NULL,                   -- provider-native agent_sessions.session_id
    project TEXT,                               -- nullable, derived from project_path
    title TEXT NOT NULL,                        -- imperative follow-up
    detail TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acted','dismissed','expired')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL DEFAULT (datetime('now','+7 days')),
    UNIQUE (session_id, title)
);

CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_commitments_session ON commitments(session_id);

CREATE TABLE IF NOT EXISTS commitment_scans (
    session_id TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL DEFAULT (datetime('now')),
    n_emitted INTEGER NOT NULL DEFAULT 0
);
