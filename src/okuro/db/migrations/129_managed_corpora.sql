-- <!-- AGENT_HEADER
-- role: code
-- purpose: 129_managed_corpora module
-- index: content
-- AGENT_HEADER_END -->
-- Managed CORPORA — the non-git sibling of managed_repos (mig 081).
--
-- repo_add answers "make this git repository searchable". It cannot answer
-- "make this Confluence space / docs folder searchable", because its whole
-- contract is a git clone url. That left every non-git knowledge source with
-- no route into cortex, and the standing temptation to build a bespoke
-- per-source search tool each time (wiki_search, notion_search, ...) — N tools
-- over N private tables, none of them reachable by the cortex_* surface agents
-- already use.
--
-- This table is the registry for the general case: an ADAPTER enumerates items
-- from some source, the items are materialised as markdown under
-- <data-dir>/corpora/<workspace>/<name>, and the directory is registered as a
-- project exactly like a clone is. Search stays cortex_search(project=...) —
-- no new query surface, no second embedding space.
--
-- Deliberately NOT reusing managed_repos: half its columns are git nouns
-- (default_branch, last_indexed_sha, pr_username) that no corpus can populate,
-- and overloading them would make "which repos are broken" unanswerable.
--
-- Columns that differ from managed_repos and why:
--   source_type  adapter id ('confluence' | 'local_folder'). The dispatch key.
--   source_ref   what the adapter points AT — a base url or an absolute path.
--   config       adapter-specific JSON (space_key, include globs, ...). Kept as
--                one blob so a new adapter needs no migration.
--   materialized 0 = the corpus IS the source dir (local_folder — nothing is
--                copied, `path` points at the user's own folder), 1 = okuro
--                wrote the files under corpora/. Governs whether remove_corpus
--                may ever delete files: deleting a user's own folder is not
--                okuro's call, so removal never touches an unmaterialized path.
--   item_count   items at last successful sync — the honest "how big is this".
--
-- No version column here: per-item version tokens live in the on-disk manifest
-- (.okuro-corpus.json) beside the files, because that is the unit delta sync
-- compares. Keeping them in a table would mean a row per page (363 for one
-- space) for data only the sync loop reads, and it would drift from the files
-- whenever a sync is interrupted mid-write. The manifest cannot drift: it is
-- rewritten in the same step as the files it describes.

CREATE TABLE IF NOT EXISTS managed_corpora (
    id             TEXT PRIMARY KEY,           -- <workspace>__<name>, mirrors repo_id
    source_type    TEXT NOT NULL,              -- adapter id
    source_ref     TEXT NOT NULL,              -- base url | absolute path
    workspace      TEXT NOT NULL DEFAULT 'default',
    name           TEXT NOT NULL,
    path           TEXT NOT NULL,              -- indexed directory on disk
    materialized   INTEGER NOT NULL DEFAULT 1,
    config         TEXT,                       -- JSON, adapter-specific
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending|fetching|indexing|ready|error
    project_id     TEXT,                       -- projects.id → cortex root
    token_key      TEXT,                       -- keyring entry name, never a secret
    username       TEXT,
    item_count     INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    last_change    TEXT,                       -- JSON {added,updated,removed,unchanged}
    last_synced_at TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Sync loops and the /corpora overview both filter by status; workspace is the
-- grouping axis in every listing. Neither is the primary key, so both earn an
-- index (unlike migration 128's columns, which are read on an already-resolved
-- row).
CREATE INDEX IF NOT EXISTS idx_managed_corpora_status
    ON managed_corpora(status);
CREATE INDEX IF NOT EXISTS idx_managed_corpora_workspace
    ON managed_corpora(workspace, name);

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP INDEX IF EXISTS idx_managed_corpora_workspace;
--   DROP INDEX IF EXISTS idx_managed_corpora_status;
--   DROP TABLE IF EXISTS managed_corpora;
