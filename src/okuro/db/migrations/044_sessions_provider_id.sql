-- <!-- AGENT_HEADER
-- role: code
-- purpose: 044_sessions_provider_id — add provider_session_id column to
--   sessions_inline so spawn-per-turn adapters (gemini, codex) can resume
--   the upstream CLI's own session identifier across turns.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, nullable. Wave 3b ships the gemini + codex CLI adapters, both
-- of which run "spawn a fresh subprocess per user turn" rather than the
-- long-lived stream-json pattern claude uses. The CLI assigns (codex) or
-- accepts (gemini) a session identifier on the first spawn; subsequent
-- turns must hand the same id back so the model sees the same context.
--
--   gemini — okuro pins the value on first spawn via `--session-id <uuid>`.
--            The same uuid is reused on subsequent spawns; the CLI treats
--            it as resume-or-create. We still persist it so a backend
--            restart can replay the same id.
--   codex  — codex assigns its own thread_id on the first `thread.started`
--            event. We capture it from the JSONL stream the first time
--            we see it and use `codex exec resume <thread_id> --json` for
--            every subsequent turn.
--
-- One column covers both providers — neither needs the other's value, but
-- the meaning is identical ("upstream CLI's session/thread identifier")
-- so a single column is clearer than provider-specific siblings.
--
-- Schema 042 declared sessions_inline; this migration only adds a column.
-- We keep the foreign-keys-off / foreign-keys-on toggle for parity with
-- 042 / 043.

PRAGMA foreign_keys = OFF;

ALTER TABLE sessions_inline ADD COLUMN provider_session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_inline_provider_session_id
    ON sessions_inline(provider_session_id)
    WHERE provider_session_id IS NOT NULL;

PRAGMA foreign_keys = ON;
