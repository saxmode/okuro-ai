-- <!-- AGENT_HEADER
-- role: code
-- purpose: 005_reminders module
-- index: content
-- AGENT_HEADER_END -->
-- okuro reminder + channel tables

CREATE TABLE IF NOT EXISTS channel_config (
    name        TEXT PRIMARY KEY,
    enabled     INTEGER DEFAULT 1,
    config      TEXT DEFAULT '{}',
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channel_deliveries (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    source      TEXT,
    source_id   TEXT,
    message     TEXT DEFAULT '{}',
    status      TEXT DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','acknowledged')),
    ack_at      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_deliveries_source ON channel_deliveries(source);

CREATE TABLE IF NOT EXISTS reminders (
    id          TEXT PRIMARY KEY,
    what        TEXT NOT NULL,
    context     TEXT DEFAULT '{}',
    when_due    TEXT NOT NULL,
    urgency     INTEGER DEFAULT 3 CHECK (urgency BETWEEN 1 AND 5),
    repeat      TEXT DEFAULT '{}',
    source      TEXT DEFAULT 'user' CHECK (source IN ('user','agent','system')),
    status      TEXT DEFAULT 'pending' CHECK (status IN ('pending','active','snoozed','done','dismissed')),
    snooze_count INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(when_due);

CREATE TABLE IF NOT EXISTS reminder_cascade (
    id              TEXT PRIMARY KEY,
    reminder_id     TEXT NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    step            INTEGER NOT NULL,
    fire_at         TEXT NOT NULL,
    channels        TEXT DEFAULT '[]',
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','sent','acknowledged','skipped')),
    sent_at         TEXT,
    acknowledged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cascade_reminder ON reminder_cascade(reminder_id);
CREATE INDEX IF NOT EXISTS idx_cascade_pending ON reminder_cascade(fire_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS reminder_suggestions (
    id              TEXT PRIMARY KEY,
    source_type     TEXT CHECK (source_type IN ('thought','signal','memory','progress')),
    source_id       TEXT,
    proposed_what   TEXT NOT NULL,
    proposed_when   TEXT,
    proposed_urgency INTEGER DEFAULT 3,
    reason          TEXT,
    accepted        INTEGER,
    reminder_id     TEXT REFERENCES reminders(id),
    created_at      TEXT DEFAULT (datetime('now'))
);
