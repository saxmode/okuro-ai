-- Migration 104 — provenance on agent_memory: who wrote it, in which session,
-- from which code.
--
-- MEASURED 2026-07-19 over 2939 rows: source_agent='unknown' on 2821 (96.0%),
-- role NULL on 2939 (100%), SUM(verified)=0. The corpus cannot answer "who
-- asserted this", "was it one session or twenty", or "which version of the
-- write path produced it".
--
-- ROOT CAUSE, and why this is a schema bug rather than an agent-discipline
-- one: write_memory() has accepted `source_agent` and `role` as Python
-- parameters for a long time (sense/memory.py), but the MCP inputSchema
-- exposed only topic/content/project/confidence/supersedes. The fields were
-- therefore STRUCTURALLY UNREACHABLE from every agent that has ever written a
-- memory. 96% anonymous is not carelessness; it is the only value the tool
-- surface allowed.
--
-- session_id: NOT added to the MCP schema, deliberately. An agent cannot know
-- its own telemetry session id, and a field the caller must remember to pass
-- regresses to NULL — that is exactly how role_knowledge lost every
-- session_id before 2026-04-26. It is derived server-side from
-- session_state.current_audit_session_id(), which exists for precisely this.
--
-- code_version: the git SHA this process booted on, NOT okuro.__version__.
-- __version__ is the static string "3.0.0" and moves perhaps twice a year,
-- so it cannot distinguish the staleness this column exists to measure — a
-- server 6 commits behind writing alongside one at HEAD, same tool name, two
-- behaviours (measured 2026-07-19). A SHA makes "the corpus is the union of N
-- frozen interpreters" a queryable fact instead of a hypothesis.
--
-- NULL on legacy rows is correct and must stay readable as "unknown", not
-- backfilled to a guess: no record exists of which interpreter wrote them.
-- Any future measurement of the write path must filter to rows where
-- code_version IS NOT NULL, or it is measuring a mixture.
--
-- Rollback (SQLite forward-only): ALTER TABLE agent_memory DROP COLUMN ...; (>=3.35)

ALTER TABLE agent_memory ADD COLUMN session_id TEXT;
ALTER TABLE agent_memory ADD COLUMN code_version TEXT;

-- The two intended analyses: "which sessions wrote this cluster" (independence
-- of confirmations — a corroboration layer's precondition) and "which code
-- version produced these rows" (is a measurement of the write path measuring
-- one write path).
CREATE INDEX IF NOT EXISTS idx_agent_memory_session
    ON agent_memory (session_id) WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_memory_code_version
    ON agent_memory (code_version) WHERE code_version IS NOT NULL;
