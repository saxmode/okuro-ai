-- <!-- AGENT_HEADER
-- role: code
-- purpose: 121_sessions_generation_is_the_dispatch_epoch module
-- index: content
-- AGENT_HEADER_END -->
-- ROCK-SOLID v5 P4.1, corrected. 120 declared sessions.generation as INTEGER.
-- That was wrong, and wrong in the specific way this plan keeps deleting:
-- a second, incompatible notion of a thing that already exists.
--
-- `dispatch_epoch` is already the dispatch generation everywhere it matters —
-- an ISO timestamp on artifact_write and write_role_handover (C12), compared
-- against the supersede-sweep watermark to reject a straggler write from a
-- prior retry. P4.4's whole job is to carry THAT value into the write layer
-- and flag superseded-generation writes. An integer counter on sessions could
-- never be compared to it, so the two would have drifted apart immediately and
-- the guard would have had nothing to check against.
--
-- SQLite type affinity is advisory, so an INTEGER-declared column will happily
-- hold an ISO string — which is precisely why this is worth a migration rather
-- than a shrug: the declared type is the only thing telling the next reader
-- what belongs there, and a column that lies about its contents is how the
-- next person writes an integer into it.
--
-- Additive and nullable, like 120. Nothing writes either column yet (dispatch
-- stamping lands with 4.2), so there is no data to migrate — only the
-- declaration to correct.

ALTER TABLE sessions ADD COLUMN dispatch_epoch TEXT;

-- Same two questions 120's indexes serve, now over the column that will
-- actually be populated.
CREATE INDEX IF NOT EXISTS idx_sessions_dispatch_epoch
    ON sessions(task_id, dispatch_epoch)
    WHERE task_id IS NOT NULL;

-- `generation` from 120 is left in place and unused. Dropping it would need
-- SQLite >= 3.35 for no benefit — it is nullable, nothing reads it, and a
-- DROP COLUMN in a migration that also ADDs one is a worse failure mode than
-- a dead column. It is documented here as dead so nobody adopts it.
--
-- ROLLBACK:
--   DROP INDEX IF EXISTS idx_sessions_dispatch_epoch;
--   ALTER TABLE sessions DROP COLUMN dispatch_epoch;
