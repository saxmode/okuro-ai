-- <!-- AGENT_HEADER
-- role: code
-- purpose: 078_sparring — persistence for okuro·prism sparring SESSIONS, the
--   stateful multi-turn adversarial loop. The partner that remembers: turns
--   accumulate within a session; durable facts flow to the temporal KG so a
--   LATER session on the same topic recalls what was assumed, decided, killed.
-- index: content
-- AGENT_HEADER_END -->
--
-- A SparringSession (topic, person, turns[]) is stored as one JSON blob in
-- `state`, mirroring prism_docs (073): last-write-wins, single-user. `topic_key`
-- is the slugified topic — the join key against the KG entity `sparring/{key}`
-- that carries cross-session memory. `turn_count` and `status` are denormalised
-- columns for cheap listing without loading every session's JSON.
--
-- `sparring_events` is the append-only change-feed (same contract as
-- prism_events): every save/delete appends a row so an open sparring view can
-- tail by `seq` and live-update whether the web user or an agent mutated it.

CREATE TABLE IF NOT EXISTS sparring_sessions (
    id TEXT PRIMARY KEY,                         -- slug, unique
    topic TEXT NOT NULL,
    topic_key TEXT NOT NULL,                     -- slugify(topic) — KG recall join key
    person_id TEXT,                              -- okuro person this session spars with
    brand_id TEXT,
    state TEXT NOT NULL DEFAULT '{}',            -- JSON: full session (turns[] + meta)
    turn_count INTEGER NOT NULL DEFAULT 0,       -- denormalised, cheap list rendering
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sparring_sessions_updated ON sparring_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sparring_sessions_topic_key ON sparring_sessions(topic_key);

CREATE TABLE IF NOT EXISTS sparring_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('saved', 'deleted')),
    origin TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sparring_events_seq ON sparring_events(seq);
