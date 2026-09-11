-- Migration 112 — outbound sync state for reviews.
--
-- Reviews are captured LOCAL-FIRST (111) and forwarded to the maintainer's
-- Supabase project only when the operator has opted in. Local-first is not a
-- nicety: config can be incomplete (an install that updated into this feature
-- has no org_label yet), the machine can be offline, and the endpoint can be
-- down. In every one of those cases the review must already be safe on disk,
-- and the sync must be a separate, retryable step. Losing a complaint because
-- a settings field was blank is the one outcome this design refuses.
--
-- synced_at NULL = still queued. That single column is the whole queue: no
-- second table, no broker, and a drain is one indexed scan.
--
-- sync_attempts + sync_error exist so a permanently-rejected row (a 4xx that
-- retrying cannot fix) stops burning attempts and can be shown to the user
-- rather than silently retried forever.
--
-- Rollback (SQLite forward-only, columns are additive and nullable so an older
-- build simply ignores them):
--   -- no destructive rollback needed; to fully revert:
--   -- ALTER TABLE reviews DROP COLUMN synced_at;
--   -- ALTER TABLE reviews DROP COLUMN sync_attempts;
--   -- ALTER TABLE reviews DROP COLUMN sync_error;

ALTER TABLE reviews ADD COLUMN synced_at     TEXT;
ALTER TABLE reviews ADD COLUMN sync_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reviews ADD COLUMN sync_error    TEXT;

-- The drain query: unsynced, oldest first.
CREATE INDEX IF NOT EXISTS idx_reviews_unsynced
    ON reviews(synced_at, created_at);
