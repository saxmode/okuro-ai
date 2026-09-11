-- <!-- AGENT_HEADER
-- role: code
-- purpose: 021_sessions_pid_host module
-- index: content
-- AGENT_HEADER_END -->
-- Add process-liveness tracking to sessions: the PID that called bootstrap,
-- and the host where that process runs. Used by close_orphaned_sessions
-- to tell "still working" from "actually abandoned" without relying on the
-- okuro-tool activity gap (which misses agents doing native tool calls).
--
-- Provider-agnostic: every MCP agent has a PID. For stdio transport (the
-- common case) there's a 1:1 mapping between PID and session, so PID
-- liveness IS session liveness. For shared-process transports (HTTP MCP,
-- orchestrator), multiple sessions share one PID — the reaper detects
-- this and falls back to the activity heuristic.

ALTER TABLE sessions ADD COLUMN pid INTEGER;
ALTER TABLE sessions ADD COLUMN host TEXT;

-- Partial index: only unclosed rows are scanned by close_orphaned_sessions.
CREATE INDEX IF NOT EXISTS idx_sessions_pid_host_live
    ON sessions(host, pid) WHERE ended_at IS NULL;
