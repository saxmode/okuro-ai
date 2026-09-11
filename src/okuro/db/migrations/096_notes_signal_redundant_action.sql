-- <!-- AGENT_HEADER
-- role: code
-- purpose: 096_notes_signal_redundant_action — drop suggested_action from note
--   signals where it only restates the summary.
-- index: content
-- AGENT_HEADER_END -->
--
-- The first notes-extract build set both summary=item.text[:200] and
-- suggested_action=item.text[:120] — the same sentence twice. inbox_detail
-- composes the expandable body as `summary + "\n\n**Suggested action:** " +
-- suggested_action`, so every note signal rendered its own text twice in the
-- UI, once plain and once under a bold label that promised something new.
--
-- The extractor no longer sets suggested_action on signals: a signal is an
-- observation, and if the note had named an action the extractor would have
-- classified the item as a todo instead. signal_promote already falls back to
-- summary when suggested_action is NULL, so nothing downstream breaks.
--
-- Scoped to source='notes' AND an exact prefix match, so a note signal that
-- somehow carries a genuinely different suggested_action is preserved. The
-- prefix test (rather than equality) is what the truncation asymmetry
-- produced: 120 chars of a 200-char summary. Idempotent — reruns match zero
-- rows. No-op on any DB that never ran the first build.
--
-- Rollback: none needed (the column is nullable and the dropped value carried
-- no information not already in summary).

UPDATE signals
   SET suggested_action = NULL
 WHERE source = 'notes'
   AND suggested_action IS NOT NULL
   AND suggested_action = substr(summary, 1, length(suggested_action));
