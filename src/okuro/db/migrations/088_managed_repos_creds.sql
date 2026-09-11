-- <!-- AGENT_HEADER
-- role: code
-- purpose: 088_managed_repos_creds — persist the credential *reference* (keyring
--   key name + Basic-auth username) used to clone a managed repo, so sync can
--   re-inject the same credential without the user re-supplying it. Fixes the
--   "could not read Username" sync failure: _clone scrubs creds from .git/config
--   by design, so origin is credential-free and a bare `git pull origin` prompts
--   for a username (impossible in a background task → error).
-- index: content
-- AGENT_HEADER_END -->
--
-- Security: only the keyring KEY NAME is stored, never the token itself (the
-- token is fetched from the keyring at runtime and injected in-memory only —
-- same guarantee as before). `username` is a Basic-auth sentinel / account id
-- (e.g. a GitHub username, or an Atlassian email), not a secret.

ALTER TABLE managed_repos ADD COLUMN token_key TEXT;   -- keyring entry name (https token); NULL for public repos
ALTER TABLE managed_repos ADD COLUMN username  TEXT;   -- Basic-auth username paired with the token; NULL = per-host default
