-- <!-- AGENT_HEADER
-- role: code
-- purpose: 087_sessions_quality module
-- index: content
-- AGENT_HEADER_END -->
-- Metric 2: session QUALITY — a separate axis from protocol adherence.
-- Adherence (compliance_score) measures whether the agent followed the okuro
-- tool protocol; it says nothing about whether the work was good. Quality is
-- judged by a fast-tier LLM over the session transcript (via the CLI bridge,
-- provider-agnostic) and lives in its own columns so the two are never
-- conflated on the scorecard.
--
--   quality_score      1..5 (1 = failed/harmful, 5 = excellent), NULL = unjudged
--   quality_rationale  one-line justification from the judge
--   quality_judged_at  when the judge ran (also the "already judged" guard)
--
-- provider_compliance gains an aggregate quality row so the panel can show
-- adherence and quality side by side.

ALTER TABLE sessions ADD COLUMN quality_score INTEGER;
ALTER TABLE sessions ADD COLUMN quality_rationale TEXT;
ALTER TABLE sessions ADD COLUMN quality_judged_at TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_quality_unjudged
    ON sessions(started_at)
    WHERE quality_score IS NULL;

ALTER TABLE provider_compliance ADD COLUMN avg_quality REAL;
ALTER TABLE provider_compliance ADD COLUMN quality_scored INTEGER;
