-- <!-- AGENT_HEADER
-- role: code
-- purpose: 081_managed_repos — lifecycle registry for cloned repositories. A
--   managed repo is cloned under the per-user data dir (~/.okuro/repos/
--   <workspace>/<name>), auto-registered as a project (so it becomes a cortex
--   root), then code-graph ingested. This table tracks the clone/index
--   lifecycle above the projects table; project_id links to the registry row.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a separate table (not extra columns on projects): projects is the generic
-- registry (local paths, launch config). A managed repo carries clone-specific
-- lifecycle state — remote url, workspace grouping, sync tier, index status,
-- last-indexed SHA — that only applies to repos okuro cloned and owns. Keeping
-- it separate leaves projects untouched for the many projects that are plain
-- local paths. project_id is the join back.
--
-- Path portability: `path` is the resolved absolute location on THIS machine
-- (okuro.db is per-user, never shared), computed at clone time from
-- repos.paths.repo_path() = repos_root()/<workspace>/<name>. The code never
-- hardcodes a machine path; only the resolver does, anchored on the data dir.

CREATE TABLE IF NOT EXISTS managed_repos (
    id                TEXT PRIMARY KEY,                          -- slug: <workspace>__<name>
    url               TEXT NOT NULL,                             -- clone url (https/ssh); token injected at runtime, never stored
    workspace         TEXT NOT NULL DEFAULT 'default',           -- grouping bucket
    name              TEXT NOT NULL,                             -- repo dir name
    path              TEXT NOT NULL,                             -- resolved absolute clone dir (per-machine)
    tier              TEXT NOT NULL DEFAULT 'air'
                          CHECK (tier IN ('air', 'advanced', 'pro')),
    status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'cloning', 'indexing', 'ready', 'error')),
    last_indexed_sha  TEXT,                                      -- HEAD commit at last successful ingest
    error             TEXT,                                      -- last error message (status='error')
    default_branch    TEXT,                                      -- resolved after clone
    project_id        TEXT,                                      -- FK -> projects.id (the auto-registered project)
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_managed_repos_workspace ON managed_repos(workspace);
CREATE INDEX IF NOT EXISTS idx_managed_repos_status ON managed_repos(status);
