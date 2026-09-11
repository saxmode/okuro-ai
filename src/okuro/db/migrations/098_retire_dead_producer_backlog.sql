-- <!-- AGENT_HEADER
-- role: code
-- purpose: 098_retire_dead_producer_backlog — clear the inbox of rows written
--   by two producers that measurably never converted.
-- index: content
-- AGENT_HEADER_END -->
--
-- Behavioural evidence over the full history (DP07 — observe, don't ask),
-- measured 2026-07-15 against the live DB:
--
--   orchestrator sentinel → thoughts : 657 rows. Heartbeat telemetry
--     ("'Task label' — step 3.1 'truncated desc…' running 121min (role: x).
--     May be stuck.") written into the user's thought stream. 16.7% of his
--     entire inbox; 734 of 990 thoughts sit dismissed. Never addressed to him.
--     Producer fixed in the same change: Sentinel.log_observations now appends
--     to the task's own .activity.jsonl.
--
--   reminder_suggestions : 111 rows. 21 explicitly REJECTED, 90 ignored,
--     0 ever accepted. Auto-generation removed from sense/reminders/eval.py.
--
-- Both producers are stopped at source; this retires the backlog they already
-- wrote, which would otherwise sit in the inbox forever — reduce_once only
-- pulls thoughts with status='open' and suggestions with accepted IS NULL.
--
-- NOT a delete. Nothing is destroyed:
--   * thoughts → status='dismissed' (a real value in the CHECK constraint;
--     the row, its content and its embedding all remain, and it stays
--     searchable via read_memory / thought search).
--   * reminder_suggestions → accepted=0, the same value the user's own
--     rejections carry.
-- The inbox overlay rows are not touched here — the next reduce_once pass
-- supersedes them automatically once their producer is no longer active.
--
-- KNOWN COST, accepted deliberately: reminder_suggestions has no audit column
-- (id, source_type, source_id, proposed_what, proposed_when, proposed_urgency,
-- reason, accepted, reminder_id, created_at — verified via PRAGMA), so after
-- this runs the 21 the user rejected by hand are indistinguishable from the 90
-- retired here. Adding a column to a feature being switched off is bloat
-- (DP09), so the counts are recorded HERE instead, which is where a future
-- reader will look: at 2026-07-15, accepted=0 → 21 user rejections, 90
-- migration retirements, accepted=1 → 0. That is the behavioural evidence
-- justifying the switch-off, preserved in the only place it still matters.
--
-- Scoped tightly: only project='orchestrator' AND category='observation'
-- thoughts. A hand-written thought that happens to be tagged to the
-- orchestrator project is NOT an observation and is left alone.
--
-- Idempotent — reruns match zero rows.
--
-- Rollback:
--   UPDATE thoughts SET status='open' WHERE project='orchestrator'
--     AND json_extract(metadata,'$.category')='observation';
--   UPDATE reminder_suggestions SET accepted=NULL WHERE accepted=0;
--   (the second over-restores the user's own 21 rejections — see KNOWN COST)
--
-- Activation: inbox rows are a stored projection — the reducer must re-run
-- (daemon `inbox-reduce`, every 30m, or POST /api/inbox/reduce).

UPDATE thoughts
   SET status = 'dismissed',
       updated_at = datetime('now')
 WHERE project = 'orchestrator'
   AND status = 'open'
   AND json_extract(metadata, '$.category') = 'observation';

UPDATE reminder_suggestions
   SET accepted = 0
 WHERE accepted IS NULL;
