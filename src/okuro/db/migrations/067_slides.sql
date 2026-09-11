-- <!-- AGENT_HEADER
-- role: code
-- purpose: 067_slides — persistence for okuro-slides, the recipient-tailored
--   deck builder. Mirrors flow_designer (064): one JSON blob per deck + an
--   append-only change-feed for cross-process live sync.
-- index: content
-- AGENT_HEADER_END -->
--
-- A deck (size, transition, background, arrangement, slides[]) is stored as one
-- JSON blob in `deck`. arrangement is denormalised to a column (it drives the
-- smart-animate off-state direction and is useful for list/filter). slide_count
-- is denormalised for cheap list rendering.
--
-- `slides_events` is the append-only change-feed: both the web process (user
-- edits) and the stdio MCP process (agent / chat edits) write to it on every
-- save/delete, and the web SSE endpoint tails it by `seq` so an open deck
-- live-updates when ANY process mutates it. `origin` carries the writer's
-- client id so a client can ignore the echo of its own write.

CREATE TABLE IF NOT EXISTS slides_decks (
    id TEXT PRIMARY KEY,                                  -- slug, unique
    title TEXT NOT NULL,
    arrangement TEXT NOT NULL DEFAULT 'horizontal'
        CHECK (arrangement IN ('horizontal', 'vertical')),
    deck TEXT NOT NULL DEFAULT '{}',                      -- JSON: full Deck IR
    slide_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_slides_decks_updated ON slides_decks(updated_at DESC);

CREATE TABLE IF NOT EXISTS slides_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('saved', 'deleted')),
    origin TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_slides_events_seq ON slides_events(seq);
