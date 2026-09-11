-- <!-- AGENT_HEADER
-- role: code
-- purpose: 127_person_sources_lens_key module
-- index: content
-- AGENT_HEADER_END -->
-- Lens writes can finally enter the ledger.
--
-- cognitive.lenses is nested ONE LEVEL DEEPER than anything else tracked:
-- {lens_key: {axis: value}}, where every other tracked field is
-- {key: value}. Migration 123 gave person_sources an `axis` column, which
-- made a two-level field ({sliders: {axis: value}}) replayable. A lens is
-- three levels, and replaying it with only bucket.key + axis would set
--
--     cognitive["lenses"] = {axis: value}
--
-- flattening away every lens_key at once — the same corruption shape the
-- two-segment field_path rule exists to prevent, one level down.
--
-- So person_update_sliders has been REFUSING to write a ledger row for lens
-- edits since P2.5, and lenses have been traced by the history archive
-- alone. The archive records what changed but never replays, so a lens edit
-- could not be reverted by remove_source the way every other write can.
-- Fable's v1 plan wanted one ledger covering every write; this is the last
-- write path that was not covered.
--
-- WHY A COLUMN AND NOT A DEEPER field_path. Identical reasoning to 123:
-- field_path stays exactly bucket.key because the rebuild splits on the
-- first dot. 'cognitive.lenses.work' would write the literal key
-- "lenses.work" into persons.cognitive beside the real dict. The
-- coordinates that do not fit the path go in columns.
--
-- ADDITIVE and DROP-SAFE: nullable, every existing row keeps lens_key NULL
-- and its current two-level semantics unchanged. A reader that has never
-- heard of the column is unaffected.
-- Rollback: ALTER TABLE person_sources DROP COLUMN lens_key; on
-- SQLite >= 3.35, or leave it — nothing requires it to be populated.

ALTER TABLE person_sources ADD COLUMN lens_key TEXT;

-- The rebuild asks "surviving applied rows for this person + field_path",
-- now grouped by lens_key as well as axis.
CREATE INDEX IF NOT EXISTS idx_person_sources_person_field_lens
    ON person_sources (person_id, field_path, lens_key, axis, created_at);
