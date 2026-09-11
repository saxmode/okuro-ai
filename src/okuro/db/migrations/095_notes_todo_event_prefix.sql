-- <!-- AGENT_HEADER
-- role: code
-- purpose: 095_notes_todo_event_prefix — backfill the notes-extract producer
--   prefix onto todos minted by the first cut of the extractor, so the inbox
--   reducer stops filing the user's own commitments as firehose.
-- index: content
-- AGENT_HEADER_END -->
--
-- The first notes-extract build set todos.source_event_id to a bare note id.
-- The inbox reducer treats raw `source='ingress'` as a scraped firehose —
-- kind='research', type_weight 0.3, surface cap 3 — and only promotes
-- producers whose source_event_id starts with an entry in
-- _CURATED_SAVE_PREFIXES (inbox/reducer.py). A bare uuid matches nothing, so
-- every note-derived todo was buried at the bottom of the inbox and mostly
-- never surfaced. The extractor now writes 'notes-extract:<note_id>'; this
-- backfills the rows written before that.
--
-- Scoped by the note_extractions ledger (created in 094) rather than by
-- `source='ingress'` alone: other ingress producers legitimately use bare
-- event ids and must not be rewritten. The NOT LIKE guard makes it
-- idempotent, and on any DB that never ran the buggy build it matches zero
-- rows and is a no-op.
--
-- Rollback: UPDATE todos SET source_event_id = REPLACE(source_event_id,
--   'notes-extract:', '') WHERE source_event_id LIKE 'notes-extract:%';
--
-- Activation: inbox rows are a stored projection — the reducer must re-run
-- for the new kind to take effect (daemon task `inbox-reduce`, every 30m, or
-- POST /api/inbox/reduce).

UPDATE todos
   SET source_event_id = 'notes-extract:' || source_event_id
 WHERE source = 'ingress'
   AND source_event_id IS NOT NULL
   AND source_event_id NOT LIKE 'notes-extract:%'
   AND id IN (
       SELECT target_id FROM note_extractions
        WHERE routed_to = 'todos' AND target_id IS NOT NULL
   );
