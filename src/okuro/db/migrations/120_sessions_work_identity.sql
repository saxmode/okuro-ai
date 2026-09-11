-- <!-- AGENT_HEADER
-- role: code
-- purpose: 120_sessions_work_identity module
-- index: content
-- AGENT_HEADER_END -->
-- ROCK-SOLID v5 P4.1 — give a session a WORK identity.
--
-- The sessions table records who is running (provider, pid, host, agent) and
-- when, but not WHAT they were dispatched to do. `create_session` takes no
-- task or subtask argument, so the question the whole of P4 turns on —
-- "is a session live for subtask 1.2 of task X right now?" — has no answer.
--
-- Everything downstream of that gap is a workaround. Dispatch cannot refuse a
-- second session for a subtask that already has one, because it cannot see the
-- first. A successor engine cannot reap its predecessor's subagents, because it
-- cannot enumerate them by task. A write arriving from a superseded dispatch
-- looks identical to one from the current dispatch, so the reviewer grades
-- whatever landed last. That is the dual-session / ghost-write / blind-grading
-- family, and it is one missing key wearing three hats.
--
-- `generation` is the dispatch epoch: the same (task, subtask) re-dispatched
-- after a retry or an engine restart gets a higher number, so "which run does
-- this write belong to?" is answerable without timestamps or heuristics.
--
-- Additive and nullable throughout. Existing rows keep NULL and read as
-- "unknown work", which is exactly what they are — there is no backfill that
-- could honestly reconstruct it.

ALTER TABLE sessions ADD COLUMN task_id TEXT;
ALTER TABLE sessions ADD COLUMN subtask_id TEXT;
ALTER TABLE sessions ADD COLUMN generation INTEGER;

-- The lease question, asked on every dispatch: "is anything live for this
-- (task, subtask)?" Partial so the index stays small — the overwhelming
-- majority of sessions are interactive CLI sessions with no work identity.
CREATE INDEX IF NOT EXISTS idx_sessions_work
    ON sessions(task_id, subtask_id)
    WHERE task_id IS NOT NULL;

-- The reap question, asked once per successor-engine boot: "what did my
-- predecessor leave running for this task?" Separate from the index above
-- because it is queried by task alone, without a subtask.
CREATE INDEX IF NOT EXISTS idx_sessions_task_open
    ON sessions(task_id, ended_at)
    WHERE task_id IS NOT NULL;

-- ROLLBACK, as a statement rather than a file: this repo has 117 migrations
-- and zero rollback files, and a lone one nobody runs is worse than a
-- documented command. The columns are additive and nullable, so reverting is
-- safe and needs no data motion:
--
--   DROP INDEX IF EXISTS idx_sessions_task_open;
--   DROP INDEX IF EXISTS idx_sessions_work;
--   ALTER TABLE sessions DROP COLUMN generation;
--   ALTER TABLE sessions DROP COLUMN subtask_id;
--   ALTER TABLE sessions DROP COLUMN task_id;
--
-- (SQLite >= 3.35 for DROP COLUMN; below that, leaving the columns in place
-- is harmless — every reader treats NULL as "unknown work".)
