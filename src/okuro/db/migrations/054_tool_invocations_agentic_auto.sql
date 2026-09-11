-- <!-- AGENT_HEADER
-- role: code
-- purpose: 054_tool_invocations_agentic_auto — extend approval_status enum
--   with 'agentic_auto' so agentic-bypass writes can be audited.
-- index: content
-- AGENT_HEADER_END -->
-- Add 'agentic_auto' to the approval_status CHECK constraint on
-- tool_invocations. SQLite does not support ALTER on CHECK constraints,
-- so we rebuild the table.
--
-- Background: migration 053 added sessions_inline.is_agentic and
-- mcp/_registry.py:381 now skips the approval-gate consult for agentic
-- sessions (correct — there is no human at the gate for subagents).
-- BUT the bypass branch ALSO skipped the tool_invocations INSERT that
-- the consult path normally writes via approval_gate.insert_invocation.
-- Result: a 2h orchestrator subagent run produces zero observability —
-- the audit table stays frozen at the row count it had before the
-- subagent spawned, gate-bypass regressions become invisible.
--
-- Theme I fix: agentic-bypass writes a tool_invocations row with the
-- new 'agentic_auto' approval_status. The existing audit views, cost
-- dashboard, and approval UI filter on approval_status — they'll see
-- the row immediately. Existing 'auto' (bridge-tier auto-approve for
-- HUMAN sessions) stays distinct so the two paths remain separable.
--
-- Rebuild strategy: rename existing table, recreate with extended
-- CHECK, copy rows back, drop the rename, restore indexes. Done inside
-- a transaction so a crash mid-migration leaves the original table
-- untouched.

BEGIN;

ALTER TABLE tool_invocations RENAME TO tool_invocations__pre054;

CREATE TABLE IF NOT EXISTS tool_invocations (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions_inline(id) ON DELETE CASCADE,
    tool_name         TEXT NOT NULL,
    args              TEXT NOT NULL DEFAULT '{}',
    approval_status   TEXT NOT NULL
                      CHECK (approval_status IN (
                          'auto', 'approved', 'denied', 'edited', 'timeout',
                          'agentic_auto'
                      )),
    approved_at       TEXT,
    executed_at       TEXT,
    result            TEXT,
    error             TEXT,
    cost_ms           INTEGER
);

INSERT INTO tool_invocations
    SELECT id, session_id, tool_name, args,
           approval_status, approved_at, executed_at,
           result, error, cost_ms
      FROM tool_invocations__pre054;

DROP TABLE tool_invocations__pre054;

CREATE INDEX IF NOT EXISTS idx_tool_invocations_session
    ON tool_invocations(session_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_invocations_tool
    ON tool_invocations(tool_name);

COMMIT;
