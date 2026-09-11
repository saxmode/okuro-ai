-- <!-- AGENT_HEADER
-- role: code
-- purpose: 133_surface_visits — remember when the user last LOOKED at a
--   surface, so a screen can render "what changed since you were here" instead
--   of restating its whole state every time.
-- index: content
-- AGENT_HEADER_END -->
--
-- WHY THIS EXISTS. The RESUME BOARD's Zone 1 is a DIFF, not a report: it shows
-- only the projects whose activity moved since the last visit, and a project
-- that did not move is simply absent. That requires one fact nothing in okuro
-- recorded — when the human last looked.
--
-- WHY A TABLE AND NOT A COLUMN ON `projects`. The question "when did you last
-- look at this" is asked by every surface, not by the projects board alone
-- (the inbox already reconstructs a weaker version of it from
-- inbox_impressions). A `projects.last_seen_at` column would answer it for one
-- screen and force the next one to invent its own storage. Keyed by SURFACE,
-- it answers for all of them, and a new surface costs a string rather than a
-- migration.
--
-- WHY NOT localStorage. okuro is read from the desktop app AND a browser, and
-- per-origin storage would give each one its own idea of when "last time" was
-- — so the same change would show as new twice, or not at all. The visit is a
-- fact about the user, not about the client that rendered it.
--
-- GRANULARITY IS DELIBERATELY THE SURFACE, NOT THE ROW. Marking each project
-- seen individually would need a viewport/intersection notion to be honest,
-- and a scroll is not a reading. One timestamp per surface is a claim the UI
-- can actually support: "you opened this board at T".

CREATE TABLE IF NOT EXISTS surface_visits (
    -- Free-form surface id ('projects', 'inbox', ...). Not an enum: adding a
    -- surface must not require a migration.
    surface       TEXT PRIMARY KEY,

    -- When the user last OPENED this surface. UTC, SQLite datetime('now')
    -- shape, matching every other timestamp in the store so `_age_days` and
    -- plain string comparison both work on it.
    last_seen_at  TEXT NOT NULL,

    -- The visit BEFORE last_seen_at. The board needs a reference point that
    -- stays put while you are reading: if the ribbon compared against
    -- last_seen_at and the same request advanced it, opening the board would
    -- erase the delta you opened it to see, and a refresh would show nothing.
    -- So a visit SHIFTS last_seen_at into here and the ribbon reads this.
    previous_seen_at TEXT,

    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
