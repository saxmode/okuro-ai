-- Migration 106 — canonicalize role tiers to the provider-agnostic set.
--
-- MEASURED 2026-07-22: roles.tier held 4 values outside the canonical set
-- {fast, standard, strategic}: specialist (18), support (1), quality (1),
-- NULL (2). None of these exist in any provider's tier_map, so the resolver
-- (orchestrator/config.py get_cli_command_parts) fell through
-- `tier_map.get(tier, tier_map["standard"])` and silently ran them all as
-- the STANDARD model — on the default claude CLI that is `sonnet`.
--
-- ROOT CAUSE: the same class as the eichi_roles bug — a value the write path
-- accepts but the resolver cannot map, degrading silently instead of failing
-- loud. Tier must be provider-agnostic and every stored value must resolve.
--
-- REMAP is evidence-based, keyed on the (now-vestigial) `model` column which
-- recorded each role's built-for intent BEFORE this migration drops nothing:
--   model = 'opus'   -> strategic  (restores 4 roles wrongly downgraded to sonnet)
--   model = 'sonnet' -> standard   (no effective-model change; already sonnet)
--   model = NULL     -> standard   (no signal; equals today's fallback)
--
-- Net effect: 18 roles unchanged (sonnet -> standard -> sonnet); 4 roles
-- corrected (sonnet -> strategic -> opus): okuro-orchestrator-engineer,
-- prism-critic, retrieval-systems-engineer, visual-communication-expert.
--
-- Idempotent: re-running matches zero rows once no rogue tiers remain.

-- 1. Opus-intended roles -> strategic (the only opus rogue rows are
--    specialist/opus x3 and quality/opus x1).
UPDATE roles
   SET tier = 'strategic'
 WHERE tier IN ('specialist', 'quality')
   AND model = 'opus';

-- 2. Every remaining non-canonical tier (specialist/sonnet, support/sonnet,
--    NULL) -> standard. Runs after step 1, so the opus rows are already
--    'strategic' and excluded. 'critical' is listed as canonical for
--    forward-compatibility even though no role carries it yet.
UPDATE roles
   SET tier = 'standard'
 WHERE tier IS NULL
    OR tier NOT IN ('fast', 'standard', 'strategic', 'critical');
