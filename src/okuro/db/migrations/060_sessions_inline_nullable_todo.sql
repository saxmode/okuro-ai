-- <!-- AGENT_HEADER
-- role: code
-- purpose: 060_sessions_inline_nullable_todo — drop NOT NULL on sessions_inline.todo_id and soft-reap legacy stream-stub todos.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why this migration exists
--   sessions_inline.todo_id was declared NOT NULL (042). An inline session
--   opened WITHOUT a parent todo (orchestrator/dispatcher_streaming and
--   bridge.mcp_tools.bridge_stream_start both pass todo_id=None) could not
--   satisfy that FK, so the registry minted a synthetic placeholder todo
--   (id `stream-stub-<session[:8]>`, title `inline session stub <hex>`,
--   status='doing') purely to hold the FK. Those stubs flooded the todo
--   inbox — 76 of them on the live DB. The registry no longer inserts them
--   (see bridge/streaming/registry.py); this migration makes the column
--   nullable so the None-path can legitimately store NULL, and soft-reaps
--   the existing stubs.
--
--   No backend JOIN relies on a non-null todo_id; the FE inline-api type is
--   already optional; there is no UNIQUE on todo_id — so nullable is safe.
--
-- Why a full table rebuild
--   SQLite cannot drop a NOT NULL constraint in place. We follow SQLite's
--   official table-redefinition procedure: build the new table, copy rows,
--   drop the old, rename, recreate every index. The new DDL is the live DDL
--   reproduced verbatim (all columns from 042/043/044/053 preserved) with
--   the SINGLE change `todo_id TEXT NOT NULL` -> `todo_id TEXT`. The FK
--   `REFERENCES todos(id) ON DELETE CASCADE` is kept.
--
-- FK toggle note
--   sqlite.py sets `PRAGMA foreign_keys=ON` per connection and applies each
--   .sql via executescript() in autocommit (isolation_level=None) — there is
--   no open transaction at script start, so `PRAGMA foreign_keys=OFF` here
--   takes effect (matches 042/036/041). Toggling FK off for the rebuild
--   prevents the child tool_invocations FK (-> sessions_inline.id) and the
--   parent todos FK from firing mid-rename while the table is renamed/dropped.

PRAGMA foreign_keys = OFF;

-- 1. New table — live DDL verbatim, todo_id nullable.
CREATE TABLE sessions_inline_new (
    id                   TEXT PRIMARY KEY,
    todo_id              TEXT REFERENCES todos(id) ON DELETE CASCADE,
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
    approval_overrides   TEXT NOT NULL DEFAULT '{}',
    mcp_token_hash       TEXT,
    mcp_token_expires_at TEXT,
    provider_session_id  TEXT,
    is_agentic           INTEGER NOT NULL DEFAULT 0
);

-- 2. Copy all rows, column-for-column (explicit list — no SELECT *).
INSERT INTO sessions_inline_new (
    id, todo_id, provider, model, system_prompt_hash,
    started_at, last_event_at, ended_at, status, transcript_path,
    approval_overrides, mcp_token_hash, mcp_token_expires_at,
    provider_session_id, is_agentic
)
SELECT
    id, todo_id, provider, model, system_prompt_hash,
    started_at, last_event_at, ended_at, status, transcript_path,
    approval_overrides, mcp_token_hash, mcp_token_expires_at,
    provider_session_id, is_agentic
FROM sessions_inline;

-- 3. Drop the old table and rename the new into place.
DROP TABLE sessions_inline;
ALTER TABLE sessions_inline_new RENAME TO sessions_inline;

-- 4. Recreate every index on sessions_inline (live set, verbatim).
CREATE INDEX idx_sessions_inline_todo
    ON sessions_inline(todo_id, started_at DESC);
CREATE INDEX idx_sessions_inline_active
    ON sessions_inline(status) WHERE status IN ('running','paused');
CREATE INDEX idx_sessions_inline_mcp_token_hash
    ON sessions_inline(mcp_token_hash)
    WHERE mcp_token_hash IS NOT NULL;
CREATE INDEX idx_sessions_inline_provider_session_id
    ON sessions_inline(provider_session_id)
    WHERE provider_session_id IS NOT NULL;

-- 5. Soft-reap the legacy stub todos. NOT a hard DELETE: each stub is
--    referenced by a sessions_inline row whose tool_invocations carry the
--    audit trail, and ON DELETE CASCADE would erase both. status='dropped'
--    removes them from every active-todo filter while preserving the chain.
UPDATE todos
   SET status = 'dropped', updated_at = CURRENT_TIMESTAMP
 WHERE id LIKE 'stream-stub-%' AND status <> 'dropped';

PRAGMA foreign_keys = ON;
