-- <!-- AGENT_HEADER
-- role: code
-- purpose: 123_person_sources_per_axis module
-- index: content
-- AGENT_HEADER_END -->
-- Per-AXIS provenance, without corrupting persons.cognitive.
--
-- The obvious shape — field_path = 'cognitive.sliders.jargon' — is a data
-- corruption path, not merely a style choice. sources._rebuild_person_columns
-- splits field_path on the FIRST dot only:
--
--     bucket, key = field_path.split(".", 1)     -> ("cognitive", "sliders.jargon")
--     current[bucket][key] = value               -> cognitive["sliders.jargon"]
--
-- i.e. it writes the literal string key "sliders.jargon" into the cognitive
-- blob, beside (not inside) the real `sliders` dict. Every reader keeps seeing
-- the stale vector while the ledger believes it applied the change.
--
-- So the axis goes in its own COLUMN and field_path stays exactly two
-- segments. The split keeps working unchanged, and the rebuild gains a
-- per-axis branch instead of a wholesale replay.
--
-- Until this migration, person_update_sliders had to write the MERGED
-- 17-axis dict on every patch, because a partial row would be replayed
-- wholesale by remove_source and delete every unlisted axis. With an axis
-- column a row means "this ONE axis became this value", which is what a
-- partial update actually is.
--
-- ADDITIVE and DROP-SAFE: axis is nullable, every existing row keeps
-- axis IS NULL and keeps its wholesale-replay semantics. A reader that has
-- never heard of the column is unaffected. Rollback is `ALTER TABLE
-- person_sources DROP COLUMN axis;` on SQLite >= 3.35, or simply leaving the
-- column in place — nothing requires it to be populated.

ALTER TABLE person_sources ADD COLUMN axis TEXT;

-- The rebuild asks "latest applied row for this person + field_path (+ axis)"
-- on every field it reverts, which is the hot path of remove_source.
CREATE INDEX IF NOT EXISTS idx_person_sources_person_field_axis
    ON person_sources (person_id, field_path, axis, created_at);
