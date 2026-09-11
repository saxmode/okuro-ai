-- <!-- AGENT_HEADER
-- role: code
-- purpose: 069_projects_design_profile — add the design_profile column the
--   bootstrap design-profile section already SELECTs but no migration created.
-- index: content
-- AGENT_HEADER_END -->
--
-- Code-vs-schema mismatch: build_design_profile (sense/bootstrap/sections.py)
-- reads projects.design_profile, but no migration ever added the column. Every
-- bootstrap logged "no such column: design_profile" (degraded section) and
-- test_m5_replay failed (malformed bootstrap → cache ratio below target).
-- Additive, nullable; existing rows default to NULL (handled by the fallback
-- chain in build_design_profile).

ALTER TABLE projects ADD COLUMN design_profile TEXT;
