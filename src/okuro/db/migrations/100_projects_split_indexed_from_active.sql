-- <!-- AGENT_HEADER
-- role: code
-- purpose: 100_projects_split_indexed_from_active — separate "cortex should
--   index this" from "agents should see this", which were one flag.
-- index: content
-- AGENT_HEADER_END -->
--
-- WHY THIS EXISTS. projects.active served two unrelated consumers:
--
--   INDEXING  cortex/roots.py:50  SELECT id, path ... WHERE active = 1
--             cortex/roots.py:383 deactivate_root() -> SET active = 0
--   CONTEXT   sense/bootstrap/resolver.py  WHERE active = 1  (agent context)
--             sense/projects.py            list_projects(active_only=True)
--             sense/proactive/advisor.py, sense/maintenance.py, ...
--
-- So a decision about what to SCAN silently caused AMNESIA. deactivate_root
-- is a cortex-hygiene tool — "stop indexing this duplicate/nested root" — but
-- flipping that flag also removed the project's charter, memories, progress
-- and description from every future bootstrap. The rows were never deleted;
-- they simply stopped being reachable by the layer that briefs agents.
--
-- Verified on the live DB 2026-07-15: one project went active=0 at
-- 2026-07-13 19:07:48 when a session called deactivate_root() on it to
-- retire a double-registered, nested root. That was a DEFENSIBLE indexing
-- call. Its blast radius was the sense layer: 46 of its memories have been
-- invisible to bootstrap ever since, and agents working on it have been
-- briefed as though the project had no history. Nothing surfaced this —
-- the two meanings were indistinguishable in the schema, so neither caller
-- could have known it was reading the other one's flag.
--
-- THE SPLIT
--   indexed = 1  cortex may scan this project's path
--   active  = 1  agents may see this project's context
--
-- Backfill sets indexed = active, so indexing behaviour is BYTE-IDENTICAL on
-- upgrade: every project cortex currently scans, it continues to scan; every
-- project it skips, it keeps skipping. This migration changes no behaviour on
-- its own. It only makes the two intents separately expressible, so that
-- cortex/roots.py can stop writing to the context flag.
--
-- Restoring context to projects that were deactivated for INDEXING reasons is
-- deliberately NOT done here. It is a data judgement per project, not a
-- schema fact: of the 11 currently-inactive projects, most are genuinely
-- retired (4 provisional, 3 test fixtures, 2 archived in a bulk cleanup on
-- 2026-05-19). Only one matches the deactivate_root incident. Bulk
-- reactivation would resurrect archived work — a regression wearing a
-- correction's clothes. It is restored as a separate, evidenced step.
--
-- Note its correct end state: indexed = 0 (the cortex decision was
-- right — the root WAS a nested duplicate) AND active = 1 (agents should see
-- its 46 memories). That combination was unrepresentable before this
-- migration. It is the whole point of the split.

ALTER TABLE projects ADD COLUMN indexed INTEGER DEFAULT 1;

-- Preserve today's indexing behaviour exactly.
UPDATE projects SET indexed = active;

CREATE INDEX IF NOT EXISTS idx_projects_indexed ON projects(indexed);
