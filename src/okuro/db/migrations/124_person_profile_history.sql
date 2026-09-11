-- <!-- AGENT_HEADER
-- role: code
-- purpose: 124_person_profile_history module
-- index: content
-- AGENT_HEADER_END -->
-- A slider edit is currently a destructive in-place overwrite.
--
-- persons.cognitive.slider_provenance keeps ONE entry per axis and the writer
-- replaces it, so it is a last-write-wins map, not a history. "What was this
-- axis three months ago" has no answer anywhere in the system, and neither
-- does "who changed it, and did the deck we sent last month see this value".
-- person_sources gained per-axis rows in 123, but those describe what a SOURCE
-- claimed, not what the stored profile actually transitioned between.
--
-- TYPED OPS, not a diff blob: add / update / delete are three genuinely
-- different events and collapsing them into "the value changed" loses the one
-- that matters most — a DELETE is how an axis silently stops driving output.
--
-- old_value is nullable and NULL means "there was nothing here", which is
-- exactly what distinguishes an add from an update. Both values are JSON so
-- this table works for any field_path, not only sliders.
--
-- ADDITIVE and DROP-SAFE. Nothing reads this table to make a decision; it is
-- an archive. Every reader must tolerate its absence, because a database
-- restored from before this migration will not have it, and history that
-- breaks the present is worse than no history.
-- Rollback: DROP TABLE person_profile_history;

CREATE TABLE IF NOT EXISTS person_profile_history (
    id           TEXT PRIMARY KEY,
    person_id    TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    op           TEXT NOT NULL CHECK (op IN ('add', 'update', 'delete')),
    field_path   TEXT NOT NULL,              -- always bucket.key, see 123
    axis         TEXT,                       -- NULL for whole-field changes
    old_value    TEXT,                       -- JSON; NULL means "did not exist"
    new_value    TEXT,                       -- JSON; NULL on a delete
    source       TEXT,                       -- who/what made the change
    created_at   TEXT DEFAULT (datetime('now'))
);

-- The question this table exists to answer is always scoped to one person
-- and usually to one axis, newest first.
CREATE INDEX IF NOT EXISTS idx_person_profile_history_person
    ON person_profile_history (person_id, field_path, axis, created_at);
