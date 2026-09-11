-- <!-- AGENT_HEADER
-- role: code
-- purpose: 142_distill_facet_attempts — stop the pipeline retrying an extraction that fails the same way every night.
-- index: content
-- AGENT_HEADER_END -->
--
-- The class of defect this closes: SELECTION THAT CANNOT TELL "NOT YET DONE"
-- FROM "DONE, FAILED, AND WILL FAIL AGAIN".
--
-- Migration 137 gave distill_facets a `stage` column, and the nightly
-- selection re-queues any row still at `stage='tier0' AND triage='judge'` —
-- written so a session tier-1 skipped for budget comes back the next night.
-- That clause cannot distinguish the session it was written for from a session
-- whose extraction FAILED, so a deterministic failure is retried forever.
--
-- Measured on the live store 2026-08-14, backlog loop: 57-67 sessions per
-- 1000-session chunk refused with `unparseable_facet`, the SAME ones every
-- chunk, each costing a model call to fail identically. 309 rows currently sit
-- at tier0/judge; the deterministic share of those is a standing nightly tax
-- for as long as the backlog exists.
--
-- Two columns, because "how many times" and "why" answer different questions:
-- the count decides whether to try again, and the error is what makes a parked
-- session diagnosable without re-running it.
--
-- WHY A COUNTER AND NOT A `parked` FLAG. A boolean records the conclusion and
-- throws away the evidence. With a counter, the retry ceiling is a config-free
-- constant in one place (pipeline.MAX_FACET_ATTEMPTS), raising it re-animates
-- everything below the new ceiling with no migration and no backfill, and
-- "failed twice" stays distinguishable from "failed nine times" when somebody
-- eventually looks at why.
--
-- WHY BUDGET REFUSALS MUST NOT COUNT. A session skipped because the nightly
-- token budget ran out has not been attempted — nothing was sent, nothing came
-- back. Counting it would park the backlog's tail purely for being at the back
-- of the queue, which is the opposite of what the re-queue clause exists for.
-- Only a real extraction attempt increments (corpus.record_facet_failure).
--
-- DEFAULT 0 rather than NULL: every existing row has been attempted zero times
-- as far as this column is concerned, which is the correct and conservative
-- reading — they stay eligible.

ALTER TABLE distill_facets
    ADD COLUMN facet_attempts INTEGER NOT NULL DEFAULT 0;

-- The last failure's reason, capped by convention at a short string
-- (corpus.py truncates). Overwritten rather than appended: the useful question
-- is "why is this parked", not "every way it has ever failed".
ALTER TABLE distill_facets
    ADD COLUMN facet_last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_distill_facets_attempts
    ON distill_facets(facet_attempts) WHERE facet_attempts > 0;
