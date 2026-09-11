-- Promotion to the per-response principle block becomes an ATTRIBUTE OF THE
-- PRINCIPLE instead of a tuple in a renderer.
--
-- WHY. `_PER_RESPONSE_PRINCIPLE_IDS` in sense/bootstrap/sections.py decided
-- which principles survive budget truncation and sit at the top of the packet.
-- The comment above that tuple records the same defect being patched once
-- already: DP03 ("never ask without 3 options") sat in the truncated tail,
-- which is why agents kept asking open questions at a user whose profile says
-- open questions trigger demand avoidance. DP03 was promoted by hand.
--
-- DP10 -- SYSTEMATIC-NOT-SPECIFIC, the class-vs-instance rule -- was not, and
-- its near-twin DP11 was. Two rules saying nearly the same thing, in the two
-- most different positions available; and DP10's non-application is what
-- triggered the 2026-07-29 instruction-system investigation. Same defect, one
-- rung down, still live. Promoting DP10 by editing the tuple would be the
-- third instance of the same patch.
--
-- SHAPE. NULL = not per-response. A number is both the flag and the position,
-- so promotion and placement are one data decision rather than two code edits
-- in two different places.
ALTER TABLE principles ADD COLUMN per_response_rank INTEGER;

-- Backfill the set the tuple carried, plus DP10 at the top. Ordering rationale:
-- DP10 first because it is the operative rule this whole investigation turned
-- on; then the epistemic trio that governs how a claim is established; then
-- DP03, which governs how a choice is put to the user.
UPDATE principles SET per_response_rank = 1 WHERE id = 'DP10';
UPDATE principles SET per_response_rank = 2 WHERE id = 'DP11';
UPDATE principles SET per_response_rank = 3 WHERE id = 'DP12';
UPDATE principles SET per_response_rank = 4 WHERE id = 'DP13';
UPDATE principles SET per_response_rank = 5 WHERE id = 'DP03';
