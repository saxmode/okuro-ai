-- <!-- AGENT_HEADER
-- role: code
-- purpose: 019_memory_utility_decay module
-- index: content
-- AGENT_HEADER_END -->
-- okuro memory utility-decay marker — tracks the last time a memory had
-- its confidence auto-decremented by the utility-decay daemon task
-- (Meta-Harness P8). NULL means "never decayed by utility signal."
--
-- The staleness-decay path in memory_hygiene uses last_accessed; this
-- is a separate field so the two decay mechanisms don't clobber each
-- other's idempotency windows.

ALTER TABLE agent_memory ADD COLUMN last_utility_decayed_at TEXT;
