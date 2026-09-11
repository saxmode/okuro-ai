-- <!-- AGENT_HEADER
-- role: code
-- purpose: 090_managed_repos_sync_delta — record WHAT changed on the last sync and
--   WHEN a sync last succeeded, for the /repos overview. `updated_at` moves on any
--   status write (incl. errors), so it can't answer "when was this repo last
--   actually pulled" — hence a dedicated last_synced_at set only on success.
-- index: content
-- AGENT_HEADER_END -->
--
-- last_change is a JSON blob computed from `git diff --shortstat prev..new` +
-- `git rev-list --count prev..new` at sync time: {prev_sha,new_sha,commits,
-- files,insertions,deletions}. Null until the first sync/add. commits=0 means
-- "already up to date" (no new commits pulled).

ALTER TABLE managed_repos ADD COLUMN last_synced_at TEXT;   -- ISO ts of last SUCCESSFUL sync/add (not error transitions)
ALTER TABLE managed_repos ADD COLUMN last_change    TEXT;   -- JSON delta of the last sync; NULL before first sync
