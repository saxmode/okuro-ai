-- <!-- AGENT_HEADER
-- role: code
-- purpose: 128_sessions_inline_agent_identity module
-- index: content
-- AGENT_HEADER_END -->
-- Round 2 of the same repair migration 122 made: bind the REST of the
-- caller's identity to the transport instead of to a process environment.
--
-- 122 moved task_id / subtask_id / dispatch_epoch / agent_pid / agent_host.
-- Three more identity-shaped facts stayed in os.environ and therefore stayed
-- inert on the live HTTP path, where one shared daemon serves every subagent
-- and its environment belongs to none of them:
--
--   subtask_role    arms the M5+ assigned-role HARD GATE
--                   (sense/mcp_middleware.py). `_assigned` was empty on every
--                   HTTP call, so the gate read "not a subagent" and never
--                   armed — the M4 lazy-load bypass it exists to close was
--                   re-open on the live transport. Worse than 122's three:
--                   dispatcher_streaming (how subagents actually run) passes
--                   no extra_env at all, so OKURO_SUBTASK_ROLE was not even
--                   set on the spawned CLI. Only the row can carry it.
--   agent_provider  per-provider attribution ("orch-<role>"). NOT the same as
--                   the pre-existing `provider` column, which names the stream
--                   ADAPTER ("claude" / "codex"). Two different facts, two
--                   columns.
--   session_type    "subagent" / "direct" — session-type telemetry and one
--                   input to _is_non_interactive_session.
--
-- Additive and nullable, exactly like 122: a browser Solve session with no
-- dispatch keeps NULL on all three and reads as "not dispatched work".

ALTER TABLE sessions_inline ADD COLUMN subtask_role TEXT;
ALTER TABLE sessions_inline ADD COLUMN agent_provider TEXT;
ALTER TABLE sessions_inline ADD COLUMN session_type TEXT;

-- No index. Unlike 122's columns these are never a lookup KEY — they are read
-- back on the row the bearer token already resolved, so every access is
-- already a primary-key hit.

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   ALTER TABLE sessions_inline DROP COLUMN session_type;
--   ALTER TABLE sessions_inline DROP COLUMN agent_provider;
--   ALTER TABLE sessions_inline DROP COLUMN subtask_role;
