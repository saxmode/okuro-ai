-- <!-- AGENT_HEADER
-- role: code
-- purpose: 061_inbox_overlay — unified inbox overlay table projecting producer rows (todos/signals/reminders/suggestions/thoughts) with a salience score.
-- index: content
-- AGENT_HEADER_END -->
--
-- Phase 1 of the unified Inbox. Additive-only: a single overlay table that
-- the daemon "inbox-reduce" task projects producer rows into, each carrying a
-- pure-function salience score. Producers and UI are untouched. No disposition
-- writes happen here — the reducer preserves any user-set `state`.
--
-- Naming note: the existing `inbox_events` table (idx_inbox_status/channel/
-- action_ref) is unrelated; this table's indexes are namespaced distinctly
-- (idx_inbox_salience / idx_inbox_kind_state / idx_inbox_state).

CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,                    -- "{ref_table}:{ref_id}"
    kind TEXT NOT NULL,                     -- task/research/continue/signal/reminder/forgotten
    ref_table TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    project TEXT,
    title TEXT NOT NULL,
    salience REAL NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0,
    type_weight REAL NOT NULL DEFAULT 0,
    source_trust REAL NOT NULL DEFAULT 0,
    gravity REAL NOT NULL DEFAULT 1,
    age_anchor_at TEXT,
    state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new','surfaced','snoozed','dismissed','acted','superseded','expired')),
    dedup_key TEXT,
    dup_count INTEGER NOT NULL DEFAULT 1,
    snoozed_until TEXT,
    surfaced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (ref_table, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_inbox_salience ON inbox(salience DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_kind_state ON inbox(kind, state);
CREATE INDEX IF NOT EXISTS idx_inbox_state ON inbox(state);
