-- <!-- AGENT_HEADER
-- role: code
-- purpose: 118_memory_adjudications — durable "I looked at this and it is
--   fine" verdicts for memory_stale findings, so a triaged finding stops
--   costing attention on every subsequent scan.
-- index: content
-- AGENT_HEADER_END -->
--
-- WHY. memory_stale reports, never demotes — correct, because mechanical
-- signals find candidates and do not settle meaning. But it had no memory of
-- its own: a finding an agent examined and deliberately LEFT ALONE came back
-- identical on the next run, indistinguishable from one nobody had ever read.
-- Measured 2026-07-28: after a 13-finding cleanup, the two survivors were both
-- verdicts of "the stale literal is parenthetical, the central claim holds" —
-- real findings, correctly adjudicated, and permanently re-presented as
-- outstanding work. A checker that cannot be answered is one people stop
-- reading, which is how the memory that enshrined a bug survived two months.
--
-- FOLLOWS model_discoveries (084): the scan re-runs freely and upserts, while
-- the human/agent verdict persists across runs. Signal = find; this = decided.
--
-- RE-ARMING IS THE POINT, not suppression. An adjudication records WHAT THE
-- CODE SAID at the time. If the code moves again the verdict is stale and the
-- finding must come back — "0.50 vs 0.45 is immaterial" says nothing about
-- 0.50 vs 0.90. code_value is therefore part of the identity of the decision,
-- not decoration.
--
-- Additive, idempotent, no rollback needed: dropping this table only loses
-- verdicts and re-arms every finding, which is the safe direction.

CREATE TABLE IF NOT EXISTS memory_adjudications (
    memory_id   TEXT NOT NULL,          -- agent_memory.id the finding was raised against
    kind        TEXT NOT NULL,          -- contradicted_literal | dangling_path
    subject     TEXT NOT NULL,          -- the symbol or the path — what the finding is ABOUT
    code_value  TEXT,                   -- what the code said when this was adjudicated;
                                        -- NULL for dangling_path (absence has no value)
    verdict     TEXT NOT NULL           -- only 'leave' today: "read it, it is fine".
                CHECK (verdict IN ('leave')),
    note        TEXT,                   -- WHY it was left — the part worth reading later
    adjudged_by TEXT,                   -- session or agent that decided
    adjudged_at TEXT NOT NULL DEFAULT (datetime('now')),

    -- One decision per (memory, kind, subject). A memory with two stale
    -- constants is two independent decisions; the same constant re-adjudicated
    -- replaces the old verdict rather than accumulating.
    PRIMARY KEY (memory_id, kind, subject)
);

-- The scan filters by memory_id, so lead with it.
CREATE INDEX IF NOT EXISTS idx_memory_adjudications_memory
    ON memory_adjudications (memory_id);
