-- Migration 105 — split confidence: what was ASSERTED vs what the system did to it.
--
-- MEASURED 2026-07-19: six magic confidence values hold 96.8% of the corpus.
-- That is a menu of author habits, not a belief scale. Worse, confidence RISES
-- with age (0.741 at <=7d -> 0.861 at >90d) while the ranker carries no recency
-- term, so the oldest claims outrank the newest ones on a number that was never
-- meant to encode time.
--
-- ROOT CAUSE: five distinct mechanisms write one mutable float, and none of them
-- leaves a record. The column cannot distinguish "the writer was confident" from
-- "this got reinforced eleven times" from "a decay pass ran last Tuesday":
--
--   1. write        sense/memory.py  _cap_confidence  -- clamp to 0.8 unless ratified
--   2. reinforce    sense/memory.py                   -- min(1.0, old + 0.1) on a near-dupe
--   3. supersede    sense/memory.py                   -- retired row forced to 0.1
--   4. decay        sense/memory_utility.py           -- utility-driven downward adjust
--   5. consolidate  scripts/memory_consolidate.py     -- cluster retire + canonical set
--
-- TWO COLUMNS, TWO QUESTIONS.
--
-- asserted_confidence answers "what did the writer actually claim?" It is written
-- ONCE at insert and never updated by anything. Before this, the asserted value
-- was destroyed at the moment of capping: _cap_confidence returned the clamped
-- float and nothing retained the input, so an agent asserting 0.95 and an agent
-- asserting 0.80 became indistinguishable rows. Observed live this session.
--
-- memory_confidence_events answers "what happened to it since, and which
-- mechanism did it?" Every mutation appends one row. This is the precondition
-- for step 4 of the brain repair (count INDEPENDENT confirmations rather than
-- assertions): "reinforced 11 times" is worthless without knowing whether that
-- was 11 sessions or one session echoing a bootstrap injection 11 times, and
-- session_id on the event is what separates those.
--
-- The cap stays. Its rationale (memory.py, above _AGENT_CONFIDENCE_CAP) is
-- sound: 1.0 memories once monopolised every confidence-ordered surface, and a
-- correction filed at 0.5 ranked below the error it retracted. What changes is
-- that capping is now RECORDED and REPORTED instead of silently applied and
-- logged to a stderr that stdio MCP servers send nowhere.
--
-- Legacy rows: asserted_confidence NULL, meaning "unknown, not un-asserted".
-- Do NOT backfill it from confidence — that would assert the post-mutation value
-- was the claim, which is precisely the conflation this migration exists to end.
--
-- Rollback (SQLite forward-only): ALTER TABLE agent_memory DROP COLUMN asserted_confidence; (>=3.35)

ALTER TABLE agent_memory ADD COLUMN asserted_confidence REAL;

CREATE TABLE IF NOT EXISTS memory_confidence_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT NOT NULL,
    at              TEXT NOT NULL DEFAULT (datetime('now')),
    -- NULL old_confidence marks the initial assertion (nothing preceded it).
    old_confidence  REAL,
    new_confidence  REAL NOT NULL,
    -- One of: assert | cap | reinforce | supersede | decay | consolidate.
    -- Free text on purpose: a new mechanism must be able to record itself
    -- before anyone remembers to widen a CHECK constraint.
    mechanism       TEXT NOT NULL,
    -- Which session drove the change — the independence signal for step 4.
    session_id      TEXT,
    source_agent    TEXT,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_confidence_events_memory
    ON memory_confidence_events (memory_id, at);

-- "How many DISTINCT sessions touched this claim" — the query step 4 needs.
CREATE INDEX IF NOT EXISTS idx_memory_confidence_events_session
    ON memory_confidence_events (session_id) WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_confidence_events_mechanism
    ON memory_confidence_events (mechanism, at DESC);
