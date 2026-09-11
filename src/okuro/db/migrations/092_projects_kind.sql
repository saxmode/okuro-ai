-- <!-- AGENT_HEADER
-- role: code
-- purpose: 092_projects_kind — generalize a "project" beyond code repos. Adds
--   `kind` so a charter subject can be code / hardware / domain / client /
--   product (e.g. "internal hardware" is a project with kind=hardware). Keeps
--   the projects table as the single charter-subject unit — no rename, no new
--   entity (DP09/DP10) — vocabulary decided 2026-07-11.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, nullable. Existing rows default to NULL (= unclassified); the
-- brain-clustering / charter-scribe flow assigns kinds. Validation lives at
-- the app layer (no CHECK) so the vocabulary can grow without a migration.
--
-- Rollback (SQLite forward-only): ALTER TABLE projects DROP COLUMN kind; (>=3.35)

ALTER TABLE projects ADD COLUMN kind TEXT;
