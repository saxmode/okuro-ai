-- <!-- AGENT_HEADER
-- role: code
-- purpose: 047_roles_tags — add tags column for M4 progressive disclosure slice-picker
-- index: content
-- AGENT_HEADER_END -->
-- M4 (Agent Skills): adds JSON array `tags` to roles so the dispatcher's
-- slice-picker can pre-select 3-5 relevant roles per task via deterministic
-- tag overlap before falling through to semantic match. Tags supplement
-- (do not replace) the existing domain/tier/description fields used today.
--
-- Backfill happens out-of-band via okuro.roles.skills.backfill_tags() which
-- derives a 3-6 tag set from {domain, tier, description-keywords, role_id
-- tokens}. Re-run safe (idempotent UPSERT in backfill).

ALTER TABLE roles ADD COLUMN tags TEXT DEFAULT '[]';
