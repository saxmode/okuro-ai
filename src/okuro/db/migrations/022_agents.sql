-- 022_agents.sql — first-class agent presence layer.
--
-- Before this migration, `sessions` doubled as both "work unit" (bootstrap → task
-- → session_report) and "agent presence" (is the CLI alive?). The Live-Agents
-- panel filtered ended_at IS NULL which meant agents disappeared from the UI
-- every time they called session_report, even though the underlying CLI was
-- still running and taking new instructions.
--
-- Split into two concepts:
--   agents   = presence (host, pid, heartbeat) — long-lived CLI/orchestrator process
--   sessions = work unit (bootstrap → report) — belongs to exactly one agent
--
-- Identity:
--   stdio transport → (provider, host, pid) uniquely identifies an agent
--   http  transport → Mcp-Session-Id (future; not backfilled)
--
-- Backfill: one agent per (provider, host, pid) group of existing sessions.
-- All backfill agents are created already-ended because they represent
-- historical CLIs.

CREATE TABLE IF NOT EXISTS agents (
    id                 TEXT PRIMARY KEY,
    provider           TEXT NOT NULL,
    host               TEXT NOT NULL,
    pid                INTEGER,
    transport          TEXT NOT NULL DEFAULT 'stdio',
    identity_key       TEXT NOT NULL UNIQUE,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    last_heartbeat_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at           TEXT,
    end_reason         TEXT,
    metadata           TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_agents_provider       ON agents(provider);
CREATE INDEX IF NOT EXISTS idx_agents_host_pid_live  ON agents(host, pid) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_agents_heartbeat_live ON agents(last_heartbeat_at DESC) WHERE ended_at IS NULL;

ALTER TABLE sessions ADD COLUMN agent_id TEXT REFERENCES agents(id);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);

-- Backfill: one agent row per distinct (provider, host, pid) of past sessions.
-- `identity_key` is suffixed with ':backfill' so the live runtime creates new
-- rows (with ':live' suffix) rather than reopening historical ones.
INSERT OR IGNORE INTO agents (
    id, provider, host, pid, transport, identity_key,
    started_at, last_heartbeat_at, ended_at, end_reason
)
SELECT
    lower(hex(randomblob(16))) || '-backfill',
    provider,
    host,
    pid,
    'stdio',
    provider || ':' || host || ':' || pid || ':backfill',
    MIN(started_at),
    COALESCE(MAX(ended_at), MAX(started_at)),
    COALESCE(MAX(ended_at), MAX(started_at)),
    'backfill'
FROM sessions
WHERE host IS NOT NULL AND pid IS NOT NULL
GROUP BY provider, host, pid;

UPDATE sessions
   SET agent_id = (
       SELECT a.id FROM agents a
        WHERE a.provider = sessions.provider
          AND a.host     = sessions.host
          AND a.pid      = sessions.pid
          AND a.end_reason = 'backfill'
        LIMIT 1
   )
 WHERE sessions.host IS NOT NULL
   AND sessions.pid  IS NOT NULL
   AND sessions.agent_id IS NULL;
