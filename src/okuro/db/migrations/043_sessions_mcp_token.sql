-- <!-- AGENT_HEADER
-- role: code
-- purpose: 043_sessions_mcp_token — add per-session bearer token columns to
--   sessions_inline so the inline HTTP MCP endpoint can authenticate every
--   tool call back to the originating session.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, nullable. Wave 3a (HTTP MCP + tier-based approval gate) issues
-- a one-shot bearer token at bridge_stream_start; the token's SHA-256 hash
-- is stored here so the FastAPI request handler can verify the bearer in
-- constant time without ever persisting the plaintext. The plaintext is
-- handed to the spawning CLI via --mcp-config and never written elsewhere.
--
-- Columns:
--   mcp_token_hash         — hex sha256 of the bearer (NULL if no inline
--                            HTTP MCP target was wired for this session;
--                            wave-2 sessions stay NULL).
--   mcp_token_expires_at   — ISO-8601 expiry (NULL = "until session ends").
--                            The dispatcher refuses tool calls past expiry
--                            even if the session row is still 'running'.
--
-- Schema 042 declared sessions_inline; this migration only adds columns,
-- so we keep the foreign-keys-off / foreign-keys-on toggle for parity with
-- how 042 itself was written.

PRAGMA foreign_keys = OFF;

ALTER TABLE sessions_inline ADD COLUMN mcp_token_hash TEXT;
ALTER TABLE sessions_inline ADD COLUMN mcp_token_expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_inline_mcp_token_hash
    ON sessions_inline(mcp_token_hash)
    WHERE mcp_token_hash IS NOT NULL;

PRAGMA foreign_keys = ON;
