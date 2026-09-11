-- <!-- AGENT_HEADER
-- role: code
-- purpose: 086_sessions_provider_session_id module
-- index: content
-- AGENT_HEADER_END -->
-- Add the upstream CLI's native session identifier to the compliance
-- `sessions` table. okuro mints its OWN session_id at bootstrap
-- (write_bootstrap_marker), which is DISJOINT from the provider's real
-- session uuid used everywhere else — the transcript index (agent_sessions)
-- keys on the provider uuid (the jsonl filename stem). Overlap between the
-- two id spaces was ~1 in 7900, so compliance telemetry could not join
-- transcript activity (message_count / assistant_count) at all.
--
-- Provider-agnostic: the okuro stdio MCP server is one subprocess per
-- session, spawned by the CLI, so it inherits that CLI's session env var
-- (CLAUDE_CODE_SESSION_ID and the codex/gemini/cursor equivalents). Bootstrap
-- resolves it via a provider->var map + a generic *_SESSION_ID fallback and
-- persists it here. Sessions whose provider exposes no such var (e.g.
-- claude-desktop) keep NULL and degrade gracefully.
--
-- 044 added an identically-named column to sessions_inline (the orchestrator
-- bridge's spawn-per-turn table) for a related but separate purpose. This is
-- the compliance-telemetry sibling. Additive, nullable.

ALTER TABLE sessions ADD COLUMN provider_session_id TEXT;

-- Join key to agent_sessions.session_id; only populated rows are of interest.
CREATE INDEX IF NOT EXISTS idx_sessions_provider_session_id
    ON sessions(provider_session_id)
    WHERE provider_session_id IS NOT NULL;
