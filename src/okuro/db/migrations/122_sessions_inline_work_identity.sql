-- <!-- AGENT_HEADER
-- role: code
-- purpose: 122_sessions_inline_work_identity module
-- index: content
-- AGENT_HEADER_END -->
-- Bind work identity to the TRANSPORT, not to a process environment.
--
-- Migration 120 gave `sessions` a work identity and nothing ever wrote it:
-- measured 2026-08-01, `SELECT count(*) FROM sessions WHERE task_id IS NOT
-- NULL` returned 0 over the whole table. The three writers all read
-- OKURO_TASK_ID / OKURO_SUBTASK_ID / OKURO_DISPATCH_EPOCH from os.environ in
-- the process HANDLING the MCP call, on the assumption that each subagent
-- talks to its own stdio MCP subprocess. The live transport is HTTP to one
-- shared daemon that carries none of those vars, so every read returned None
-- and the lease, the reap and the C12 epoch fence all failed silently open.
--
-- `sessions_inline` is where the assumption can be repaired, because it is the
-- row the per-session bearer token already resolves to on every single HTTP
-- request. Identity written here at spawn is readable at the boundary by any
-- transport, in any process, with an empty environment.
--
-- `agent_pid` / `agent_host` are the second half of the same defect. The reap
-- (engine.reap_predecessor_sessions) SIGTERMs `sessions.pid`, which on the
-- HTTP path is the pid of the shared daemon — so an identity fix without a pid
-- fix would have pointed the reaper at okuro itself. The pid recorded here is
-- the spawned CLI's, known only to the process that spawned it.
--
-- Additive and nullable: an inline session with no dispatch (a browser Solve
-- session) keeps NULL and reads as "not dispatched work", which is what it is.

ALTER TABLE sessions_inline ADD COLUMN task_id TEXT;
ALTER TABLE sessions_inline ADD COLUMN subtask_id TEXT;
ALTER TABLE sessions_inline ADD COLUMN dispatch_epoch TEXT;
ALTER TABLE sessions_inline ADD COLUMN agent_pid INTEGER;
ALTER TABLE sessions_inline ADD COLUMN agent_host TEXT;

-- The lease question, asked before every dispatch: "is anything live for this
-- (task, subtask)?" Partial — most inline sessions are not dispatched work.
CREATE INDEX IF NOT EXISTS idx_sessions_inline_work
    ON sessions_inline(task_id, subtask_id)
    WHERE task_id IS NOT NULL;

-- The reap question, asked once per successor-engine boot: "what did my
-- predecessor leave running for this task?"
CREATE INDEX IF NOT EXISTS idx_sessions_inline_task_status
    ON sessions_inline(task_id, status)
    WHERE task_id IS NOT NULL;

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP INDEX IF EXISTS idx_sessions_inline_task_status;
--   DROP INDEX IF EXISTS idx_sessions_inline_work;
--   ALTER TABLE sessions_inline DROP COLUMN agent_host;
--   ALTER TABLE sessions_inline DROP COLUMN agent_pid;
--   ALTER TABLE sessions_inline DROP COLUMN dispatch_epoch;
--   ALTER TABLE sessions_inline DROP COLUMN subtask_id;
--   ALTER TABLE sessions_inline DROP COLUMN task_id;
