-- 077_artifact_audience — separate the three communication streams in the
-- artifacts table so the task panel can default to USER-facing deliverables.
--
-- audience:
--   user    — a deliverable meant for the human (Stream B). Default.
--   process — QA / reviewer / M-pipeline meta output (verdicts, autofix).
--   agent   — agent-to-agent context (compressor decision-traces). These leak
--             into the panel today; they belong to the agent stream.
--
-- Backfill is conservative: only demote KNOWN process/agent-pipeline authors
-- and the compressor decision-trace pattern. Every role-authored subagent
-- deliverable (researcher, qa-engineer, solution-architect, NULL author, …)
-- stays 'user' — nothing real is hidden, only obvious machine output demoted.

ALTER TABLE artifacts ADD COLUMN audience TEXT NOT NULL DEFAULT 'user';

UPDATE artifacts SET audience = 'agent'
 WHERE created_by = 'compressor'
    OR (kind = 'plan' AND title LIKE 'Decision trace%');

UPDATE artifacts SET audience = 'process'
 WHERE audience = 'user'
   AND created_by IN (
       'reviewer-autofix', 'critic', 'scorer',
       'phase-summarizer', 'documenter', 'workforce-reviewer'
   );

CREATE INDEX IF NOT EXISTS idx_artifacts_audience ON artifacts(audience);
