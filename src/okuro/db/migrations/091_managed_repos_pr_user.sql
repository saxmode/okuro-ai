-- <!-- AGENT_HEADER
-- role: code
-- purpose: 091_managed_repos_pr_user — store the PR/REST Basic-auth identity,
--   which differs from the git push identity. On Bitbucket the git remote
--   accepts username=<git-handle> + ATATT token, but the REST API (used to open
--   a pull request) 401s for that username and only accepts the Atlassian
--   ACCOUNT EMAIL + the same token. So Push (git) and PR (REST) need two
--   different usernames against one token — hence a separate column.
-- index: content
-- AGENT_HEADER_END -->
--
-- NULL = no PR automation configured; Push will still create the branch but
-- report that it could not open the PR (open one manually). The token itself
-- stays in the keyring under token_key — only the username differs here.

ALTER TABLE managed_repos ADD COLUMN pr_username TEXT;   -- REST/PR Basic-auth user (Bitbucket = Atlassian account email)
