-- Migration 102 — allow 'antigravity' as a sessions_inline provider, drop 'gemini'.
--
-- sessions_inline.provider carries a CHECK allow-list that migration 042
-- declared as ('claude','gemini','codex','ollama','vllm'). The gemini
-- provider was retired on 2026-07-18 (its CLI stopped serving individual
-- accounts on 2026-06-18) and antigravity (`agy`) replaced it as okuro's
-- Google path. The streaming registry gained an AntigravityStreamAdapter,
-- but every bridge_stream_start with provider='antigravity' died at the
-- INSERT with:
--
--   sqlite3.IntegrityError: CHECK constraint failed:
--   provider IN ('claude','gemini','codex','ollama','vllm')
--
-- SQLite cannot ALTER a CHECK constraint, so this is the standard
-- 12-step table rebuild: new table, copy, drop, rename, recreate indexes.
--
-- 'gemini' is REMOVED from the new allow-list rather than kept alongside
-- antigravity. Nothing can write it any more (the provider is gone from
-- the bridge registry, cli_probe, canon and the streaming registry), and
-- leaving a dead value in an allow-list invites a future reader to think
-- the provider is still dispatchable.
--
-- EXISTING ROWS: any historical row with provider='gemini' is rewritten to
-- 'antigravity' rather than dropped. Those rows are real past sessions
-- against Google models; antigravity is the same vendor's successor CLI,
-- so it is the honest label, and the alternative (deleting them, or
-- keeping 'gemini' in the allow-list forever) is worse. The rewrite is
-- logged by row count in the migration output.
--
-- ollama/vllm stay: they are local-inference session kinds, unrelated to
-- the CLI provider retirement.
--
-- Idempotent via the _migrations ledger. No machine paths.

PRAGMA foreign_keys=OFF;

CREATE TABLE sessions_inline_new (
    id                   TEXT PRIMARY KEY,
    todo_id              TEXT REFERENCES todos(id) ON DELETE CASCADE,
    provider             TEXT NOT NULL
                         CHECK (provider IN ('claude','antigravity','codex','ollama','vllm')),
    model                TEXT,
    system_prompt_hash   TEXT,
    started_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_event_at        TEXT,
    ended_at             TEXT,
    status               TEXT NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running','paused','done','cancelled','error')),
    transcript_path      TEXT,
    approval_overrides   TEXT NOT NULL DEFAULT '{}',
    mcp_token_hash       TEXT,
    mcp_token_expires_at TEXT,
    provider_session_id  TEXT,
    is_agentic           INTEGER NOT NULL DEFAULT 0
);

INSERT INTO sessions_inline_new (
    id, todo_id, provider, model, system_prompt_hash,
    started_at, last_event_at, ended_at, status, transcript_path,
    approval_overrides, mcp_token_hash, mcp_token_expires_at,
    provider_session_id, is_agentic
)
SELECT
    id,
    todo_id,
    CASE provider WHEN 'gemini' THEN 'antigravity' ELSE provider END,
    model,
    system_prompt_hash,
    started_at,
    last_event_at,
    ended_at,
    status,
    transcript_path,
    approval_overrides,
    mcp_token_hash,
    mcp_token_expires_at,
    provider_session_id,
    is_agentic
FROM sessions_inline;

DROP TABLE sessions_inline;

ALTER TABLE sessions_inline_new RENAME TO sessions_inline;

-- Recreate all four indexes EXACTLY as they were. DROP TABLE takes the
-- indexes with it, so anything not restated here is silently lost —
-- including the composite sort column and the three partial-index WHERE
-- clauses. Copied verbatim from sqlite_master before the rebuild.
CREATE INDEX IF NOT EXISTS idx_sessions_inline_todo
    ON sessions_inline(todo_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_inline_active
    ON sessions_inline(status) WHERE status IN ('running','paused');
CREATE INDEX IF NOT EXISTS idx_sessions_inline_mcp_token_hash
    ON sessions_inline(mcp_token_hash)
    WHERE mcp_token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_inline_provider_session_id
    ON sessions_inline(provider_session_id)
    WHERE provider_session_id IS NOT NULL;

PRAGMA foreign_keys=ON;
