-- <!-- AGENT_HEADER
-- role: code
-- purpose: 040_integrations — ingress adapter registry. One row per channel.
--   Tracks enabled/status/last_seen/last_error for telegram (first user)
--   and future adapters (slack/discord/email). Adapter-specific config
--   lives in config_json (chat_id allowlist, polling offset, …).
-- index: content
-- AGENT_HEADER_END -->
--
-- Design notes:
--   * channel is PRIMARY KEY — one row per integration type. The schema
--     can grow without migrations for new adapter knobs (config_json is
--     opaque JSON). State columns (status / last_seen / last_error) are
--     typed because /health renders them as a table.
--   * Secrets (bot token) live in okuro.keyring under
--     integration/<channel>/<key>. This table never stores secret values.
--   * allowed_chat_ids is part of config_json — non-secret, editable
--     from the UI, single source of truth.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS integrations (
    channel         TEXT PRIMARY KEY,
    enabled         INTEGER NOT NULL DEFAULT 0,
    config_json     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'stopped'
                      CHECK (status IN ('stopped', 'starting', 'running', 'error')),
    last_seen_at    TEXT,
    last_message_at TEXT,
    last_error      TEXT,
    last_error_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_integrations_enabled ON integrations(enabled);

PRAGMA foreign_keys = ON;
