-- <!-- AGENT_HEADER
-- role: code
-- purpose: 093_projects_provisional — quarantine auto-registered project rows.
--   `_ensure_project` (the single choke point every memory/artifact/kg/progress
--   write routes through) mints a projects row for any unseen slug — the leak
--   that fills the registry with `unknown/*` dupes. This flag marks those rows
--   provisional so canonical surfaces (finder, charter eligibility) can exclude
--   them, WITHOUT rewriting any memory tag (tag reroute is a gated op).
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, default 0 (curated). Backfill flags existing junk conservatively:
-- only rows whose path is a synthetic `unknown/*` placeholder (from
-- _infer_project_path) — those cannot be real filesystem projects. Real
-- projects (concrete path) stay provisional=0. Fully reversible: the flag is
-- metadata only; curation (real path set) flips it back to 0.
--
-- Rollback (SQLite forward-only): ALTER TABLE projects DROP COLUMN provisional; (>=3.35)

ALTER TABLE projects ADD COLUMN provisional INTEGER DEFAULT 0;

UPDATE projects
   SET provisional = 1
 WHERE (path LIKE 'unknown/%' OR path IS NULL)
   AND provisional = 0;
