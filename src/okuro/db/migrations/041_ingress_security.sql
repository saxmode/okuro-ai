-- <!-- AGENT_HEADER
-- role: code
-- purpose: 041_ingress_security — pending high-stakes actions awaiting
--   challenge response + per-chat security state (verification grace,
--   lockout, attempt tracking). One row per (channel, chat_id).
-- index: content
-- AGENT_HEADER_END -->
--
-- Design:
--   * One pending action at a time per chat — new high-stakes intent
--     overwrites the previous pending (UPSERT on PK). User can't queue
--     two orchestrator tasks behind a single challenge.
--   * verified_until: when set and in the future, the chat is "trusted"
--     and high-stakes intents dispatch without a new challenge.
--   * lockout_until: when set and in the future, all high-stakes
--     intents are refused.
--   * recent_failures: JSON list of ISO timestamps inside the 60s window
--     used to trigger lockout at the threshold.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS ingress_pending_actions (
    channel         TEXT NOT NULL,
    chat_id         INTEGER NOT NULL,
    intent          TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    challenge_idx   INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_ingress_pending_expires
    ON ingress_pending_actions(expires_at);

CREATE TABLE IF NOT EXISTS ingress_chat_state (
    channel          TEXT NOT NULL,
    chat_id          INTEGER NOT NULL,
    verified_until   TEXT,
    lockout_until    TEXT,
    recent_failures  TEXT NOT NULL DEFAULT '[]',
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, chat_id)
);

PRAGMA foreign_keys = ON;
