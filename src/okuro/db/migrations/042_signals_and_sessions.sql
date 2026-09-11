-- <!-- AGENT_HEADER
-- role: code
-- purpose: 042_signals_and_sessions — proactive-signals pipeline schema.
--   Four additive changes:
--     1. `signals` — proposals from sysmon / proactive / orchestrator /
--        manual sources, waiting to become todos (or be discarded).
--     2. `todos` — additive columns linking back to the signal that spawned
--        them and carrying resolution metadata (plan, provider pref, role
--        hint, context blob, approval mode, in-flight session/orchestrator
--        claim).
--     3. `sessions_inline` — lightweight provider-attached sessions running
--        a single todo to completion (Claude/Gemini/Codex/Ollama/vLLM).
--     4. `tool_invocations` — per-tool-call audit + approval ledger for
--        sessions_inline (auto/approved/denied/edited/timeout).
-- index: content
-- AGENT_HEADER_END -->
--
-- Storage shape:
--   * SQLite. UUIDs are stored as TEXT (caller-generated, see todos.py /
--     deliveries pattern). JSON columns are TEXT (NOT NULL DEFAULT '{}').
--     Booleans are INTEGER 0/1 (NOT NULL DEFAULT 0). Timestamps are TEXT
--     ISO-8601 via datetime('now').
--   * CHECK constraints pin the closed vocabularies — drift is a deploy-
--     blocking schema change, not a silent runtime drift.
--   * Partial indexes are used where the spec calls them out — SQLite
--     supports `WHERE <expr>` on `CREATE INDEX` since 3.8.0.
--
-- Why these four together:
--   The signals → todos → sessions_inline → tool_invocations chain is one
--   logical pipeline. Splitting it across migrations would let a partial
--   apply land a `todos.source_signal_id` column whose target table does
--   not yet exist, and would also force the migration runner's contiguity
--   guard to track an artificial gap. One file, one logical change.
--
-- Foreign keys:
--   sqlite.py runs `PRAGMA foreign_keys=ON` per connection. We toggle it
--   OFF for the duration of the schema change (matches 036/041) so that
--   forward references inside `ALTER TABLE todos ADD COLUMN` don't fail
--   on a half-built schema, then re-enable on exit.

PRAGMA foreign_keys = OFF;

-- ---------------------------------------------------------------------------
-- 1. signals — proposals awaiting promotion to a todo
-- ---------------------------------------------------------------------------
--
-- A signal is a *suggestion* sourced from automation (sysmon, proactive
-- inference, orchestrator) or a human (manual). It is NOT yet actionable:
-- promotion to a `todos` row is a separate step (the link is recorded
-- via todos.source_signal_id).
--
-- evidence: JSON payload describing what was observed (metrics, file
-- paths, log excerpts). suggested_action: free-form imperative that the
-- promotion step can pre-fill todo.title with. auto_promote: if true,
-- the pipeline may bypass user approval for promotion.
--
-- expires_at: optional self-discard timestamp. The proactive pipeline
-- scans for `status='open' AND expires_at < now()` and flips them to
-- 'expired'. Discard reason is captured in discard_reason for audit.

CREATE TABLE IF NOT EXISTS signals (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL
                      CHECK (source IN ('sysmon','proactive','orchestrator','manual')),
    source_ref        TEXT,
    severity          TEXT NOT NULL
                      CHECK (severity IN ('info','warn','crit')),
    summary           TEXT NOT NULL,
    evidence          TEXT NOT NULL DEFAULT '{}',
    suggested_action  TEXT,
    auto_promote      INTEGER NOT NULL DEFAULT 0
                      CHECK (auto_promote IN (0,1)),
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','promoted','discarded','ignored','expired')),
    discard_reason    TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_status_severity_created
    ON signals(status, severity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_source
    ON signals(source);

-- ---------------------------------------------------------------------------
-- 2. todos — additive columns for the signals pipeline
-- ---------------------------------------------------------------------------
--
-- All columns are nullable (or carry a default) so existing rows survive
-- the migration untouched. `todos` is defined in 012_todos.sql with PK
-- `id TEXT` — these new columns extend the row without rewriting it.
--
-- source_signal_id: backlink to the originating signal (NULL when the
--   todo was created directly via todo_add). ON DELETE SET NULL so a
--   signal purge does not cascade into the user-visible todo list.
-- resolution_plan: how the system intends to resolve this todo —
--   'orchestrator' (multi-step plan), 'session' (single inline session),
--   'manual' (user does it).
-- provider_pref: routing hint for sessions_inline.provider when the
--   resolution_plan is 'session'. 'auto' defers to bridge routing.
-- role_hint: optional roles.id (free text — no FK; roles live in the
--   role catalog which is fixture-seeded and not schema-pinned here).
-- context_blob: JSON payload mirroring todos.context but reserved for
--   the resolution pipeline (system prompt fragments, brief refs).
--   Kept distinct from `context` to avoid mixing user-visible context
--   with execution metadata.
-- approval_mode: per-todo override of the global approval mode. Default
--   'auto-tiered' = the bridge decides per-tool. 'ask-all' = every tool
--   call prompts. 'deny-all' = no tool calls executed (dry-run-only).
-- claimed_session_id / claimed_orchestrator_id: at most one of these is
--   set when the todo is in flight. The pair is the lease — clearing it
--   releases the todo back to the pool. claimed_session_id intentionally
--   has no FK constraint to sessions_inline: the lease may briefly point
--   at a session that has just been purged, and we'd rather see a NULL
--   resolve than fight a foreign-key cascade timing bug.

ALTER TABLE todos ADD COLUMN source_signal_id TEXT
    REFERENCES signals(id) ON DELETE SET NULL;
ALTER TABLE todos ADD COLUMN resolution_plan TEXT
    CHECK (resolution_plan IS NULL OR resolution_plan IN
        ('orchestrator','session','manual'));
ALTER TABLE todos ADD COLUMN provider_pref TEXT
    CHECK (provider_pref IS NULL OR provider_pref IN
        ('claude','gemini','codex','ollama','vllm','auto'));
ALTER TABLE todos ADD COLUMN role_hint TEXT;
ALTER TABLE todos ADD COLUMN context_blob TEXT;
ALTER TABLE todos ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'auto-tiered'
    CHECK (approval_mode IN ('auto-tiered','ask-all','deny-all'));
ALTER TABLE todos ADD COLUMN claimed_session_id TEXT;
ALTER TABLE todos ADD COLUMN claimed_orchestrator_id TEXT;

CREATE INDEX IF NOT EXISTS idx_todos_source_signal
    ON todos(source_signal_id) WHERE source_signal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_todos_claimed_session
    ON todos(claimed_session_id) WHERE claimed_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_todos_claimed_orchestrator
    ON todos(claimed_orchestrator_id) WHERE claimed_orchestrator_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. sessions_inline — provider-attached sessions resolving one todo
-- ---------------------------------------------------------------------------
--
-- A sessions_inline row is the runtime handle for a single LLM-backed
-- attempt to close a todo. Distinct from `sessions` (the agent-trace
-- store, 020_agent_session_sources): that records observed CLI sessions;
-- this records sessions okuro spawned itself.
--
-- transcript_path: filesystem path to the streamed transcript (one file
--   per session, JSONL). NULL until the first event is written.
-- approval_overrides: JSON map of {tool_name: approval_mode} that
--   overrides the parent todo.approval_mode for specific tools (e.g.
--   "Bash" → "ask-all" even when the todo is auto-tiered).
-- last_event_at: updated by the streaming bridge on every event so the
--   web UI can detect stalled sessions without polling tool_invocations.

CREATE TABLE IF NOT EXISTS sessions_inline (
    id                   TEXT PRIMARY KEY,
    todo_id              TEXT NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    provider             TEXT NOT NULL
                         CHECK (provider IN ('claude','gemini','codex','ollama','vllm')),
    model                TEXT,
    system_prompt_hash   TEXT,
    started_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_event_at        TEXT,
    ended_at             TEXT,
    status               TEXT NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running','paused','done','cancelled','error')),
    transcript_path      TEXT,
    approval_overrides   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_inline_todo
    ON sessions_inline(todo_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_inline_active
    ON sessions_inline(status) WHERE status IN ('running','paused');

-- ---------------------------------------------------------------------------
-- 4. tool_invocations — per-tool-call audit + approval ledger
-- ---------------------------------------------------------------------------
--
-- Every tool call a sessions_inline makes is recorded here, regardless
-- of whether it was auto-approved, asked, denied, edited, or timed out.
-- This is the single source of truth for "what did the agent try to
-- do?" — used by the approval UI, the cost dashboard, and the audit log.
--
-- approval_status:
--   'auto'     — bridge approved without prompting (auto-tiered + safe).
--   'approved' — user explicitly approved via the approval UI.
--   'denied'   — user explicitly denied.
--   'edited'   — user approved a modified version of the args.
--   'timeout'  — prompt expired without a response (defaults to deny).
-- approved_at: stamp of the user action (NULL for 'auto'/'timeout').
-- executed_at: stamp of execution (NULL if denied/timeout). result and
--   error are mutually exclusive — set the one that applies.
-- cost_ms: wall-clock duration in milliseconds. Cents/tokens live on
--   the provider receipts in `surface_log` (024); this table is just
--   the per-call ledger.

CREATE TABLE IF NOT EXISTS tool_invocations (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions_inline(id) ON DELETE CASCADE,
    tool_name         TEXT NOT NULL,
    args              TEXT NOT NULL DEFAULT '{}',
    approval_status   TEXT NOT NULL
                      CHECK (approval_status IN ('auto','approved','denied','edited','timeout')),
    approved_at       TEXT,
    executed_at       TEXT,
    result            TEXT,
    error             TEXT,
    cost_ms           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tool_invocations_session
    ON tool_invocations(session_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_invocations_tool
    ON tool_invocations(tool_name);

PRAGMA foreign_keys = ON;
