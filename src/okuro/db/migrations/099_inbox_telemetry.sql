-- <!-- AGENT_HEADER
-- role: code
-- purpose: 099_inbox_telemetry — make inbox engagement measurable: an
--   append-only disposition log + an impression log.
-- index: content
-- AGENT_HEADER_END -->
--
-- WHY THIS EXISTS. The 2026-07-15 audit proved the inbox had surfaced nothing
-- new in 38-42 days and that only 2 of 1309 items had ever been acted on. A
-- pile of fixes followed. Whether they worked is an empirical question — and
-- okuro currently cannot answer it:
--
--   * There is no disposition timestamp. `inbox.updated_at` is overwritten by
--     every reduce pass (8 sites in reducer.py), so it records the last
--     projection, not the user's click. Live proof at the time of writing: two
--     rows the user disposed weeks ago carried updated_at = the moment an agent
--     re-ran reduce_once. The audit had to report "exact dates of the 8
--     dispositions: UNPROVEN".
--   * There is no impression signal. `surfaced_at` is when okuro put a row on
--     the shelf, NOT when the user looked at it. Without it a low act-rate is
--     uninterpretable: "he saw it and ignored it" (the items are bad) and "he
--     never opened the inbox" (the items are untested) are indistinguishable.
--     The ranking audit hit the same wall on the 42-day incumbents.
--
--   NB `inbox_events` already exists and is NOT related — it is the Telegram /
--   messaging ingress table (channel/sender/content). Do not overload it.
--
-- Both tables are APPEND-ONLY. That is the point: the reason the old data was
-- unreadable is that a mutable column got rewritten by a background job. An
-- event log cannot be clobbered by the next reduce pass.
--
-- Rollback: DROP TABLE inbox_dispositions; DROP TABLE inbox_impressions;
--   (pure telemetry — nothing reads them for behaviour, only for analysis)

-- ── dispositions ────────────────────────────────────────────────────────────
-- One row per user verdict, forever. The scoring context is denormalised on
-- purpose: to answer "was the thing he acted on ranked highly?" months later,
-- the salience AT THE MOMENT OF THE CLICK is needed — the live row's salience
-- will have decayed and been rewritten many times by then.

CREATE TABLE IF NOT EXISTS inbox_dispositions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL,          -- "{ref_table}:{ref_id}"
    ref_table    TEXT NOT NULL,
    ref_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    project      TEXT,
    action       TEXT NOT NULL CHECK (action IN ('act','defer','dismiss')),
    -- Scoring context, frozen at click time.
    salience     REAL,
    importance   REAL,
    rank_in_view INTEGER,                -- 1-based position among surfaced rows
    -- Timing: how long from okuro showing it to the user answering.
    surfaced_at  TEXT,
    age_hours    REAL,                   -- item age at disposition
    disposed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inbox_disp_at     ON inbox_dispositions(disposed_at);
CREATE INDEX IF NOT EXISTS idx_inbox_disp_kind   ON inbox_dispositions(kind, action);
CREATE INDEX IF NOT EXISTS idx_inbox_disp_item   ON inbox_dispositions(item_id);

-- ── impressions ─────────────────────────────────────────────────────────────
-- One row per inbox READ, not per item — a list fetch is a single act of
-- attention, and per-item rows would outnumber dispositions ~1000:1 for no
-- extra insight. `item_ids` keeps the JSON array actually returned so
-- "was it on screen when he ignored it?" stays answerable.
--
-- This is an API-level proxy: it records that the list was fetched and what it
-- contained. It cannot prove eyes-on-screen — no frontend viewport tracking —
-- but it cleanly separates "never fetched" from "fetched and ignored", which
-- is the distinction the audit could not make.

CREATE TABLE IF NOT EXISTS inbox_impressions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    state      TEXT,                     -- which view: surfaced / staging / all
    kind       TEXT,                     -- kind filter, if any
    n_items    INTEGER NOT NULL DEFAULT 0,
    item_ids   TEXT NOT NULL DEFAULT '[]',
    viewed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inbox_impr_at ON inbox_impressions(viewed_at);
