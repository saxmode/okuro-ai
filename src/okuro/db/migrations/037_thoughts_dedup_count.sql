-- Migration 037 — split capture_thought dedup-bump from real surface count.
--
-- Before: capture_thought's dedup path bumped thoughts.surface_count when an
-- incoming observation collapsed onto an existing row. surface_count was
-- ALSO the signal the daily_digest "Forgotten Ideas" bucket gated on
-- (surface_count = 0). Result: a daemon-side polling watcher that re-emitted
-- the same observation every 5 min would silently push its target thought
-- out of the forgotten bucket — even though no human ever saw it.
--
-- Fix: introduce thoughts.dedup_count as the home for capture-time bumps,
-- and reserve surface_count strictly for "the thought was actually shown
-- to a consumer" signals (bootstrap, daily_digest, search_thoughts).
--
-- Migration semantics: dedup_count starts at 0 for all rows. Existing
-- surface_count values stay where they are (we cannot retroactively split
-- them, so historical counts remain a hybrid signal — only NEW captures
-- write into dedup_count). Forgotten-bucket math becomes accurate from
-- this point forward.

ALTER TABLE thoughts ADD COLUMN dedup_count INTEGER DEFAULT 0;
