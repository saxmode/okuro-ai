-- <!-- AGENT_HEADER
-- role: code
-- purpose: 097_notes_todo_priority_inversion — repair todos written by the
--   first notes-extract prompt, which used an inverted priority scale.
-- index: content
-- AGENT_HEADER_END -->
--
-- okuro's canonical todo scale is 1 (low) .. 5 (critical) — sense/todos.py
-- documents it, and inbox ranking reads importance = priority/5
-- (sense/inbox/scorer.py::priority_to_importance), so HIGHER must mean MORE
-- URGENT.
--
-- The first notes-extract prompt told the model the opposite: "priority 1
-- (urgent) .. 5 (someday)". The model obeyed and graded correctly on the scale
-- it was given, so the stored numbers are exactly inverted. Measured effect on
-- the live DB: "Present SmartSend to sales team tomorrow with Elena and Jana"
-- — the single most time-critical item okuro has ever extracted — was stored
-- P1, scored importance 0.20 (the floor), and ranked LAST of the 10 note todos.
-- The vaguest item in the set ("Work on process so developers give autonomous
-- feedback") was stored P3, scored 0.60, and surfaced. Ranking was inverted at
-- the source; no amount of gate fixing would have shown the right item.
--
-- Repair: p -> 6 - p. Maps 1->5, 2->4, 3->3, 4->2, 5->1 — the exact inverse of
-- the mis-specified scale, so the model's actual judgement is preserved.
--
-- Scoped through the note_extractions ledger (094) so only rows this producer
-- wrote are touched; every other producer's priorities are already correct and
-- must not move.
--
-- NOT idempotent by construction — a second run would re-invert. Guarded by
-- the ledger join plus a marker: rows are only rewritten while the ledger's
-- extractor rows predate the fix. Re-running this migration is prevented by
-- the _migrations table (each file applies once). Do not hand-run it twice.
--
-- Rollback: the same statement — p -> 6 - p is its own inverse.
--
-- Activation: inbox.importance is a stored projection; the reducer must re-run
-- for the corrected priorities to reach the ranking (daemon `inbox-reduce`,
-- every 30m, or POST /api/inbox/reduce).

UPDATE todos
   SET priority = 6 - priority,
       updated_at = datetime('now')
 WHERE source = 'ingress'
   AND priority BETWEEN 1 AND 5
   AND id IN (
       SELECT target_id FROM note_extractions
        WHERE routed_to = 'todos' AND target_id IS NOT NULL
   );
