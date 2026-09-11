-- <!-- AGENT_HEADER
-- role: code
-- purpose: 085_model_discovery_commercial — record the commercial-use verdict
--   on each discovery at qualification time. The weekly scan resolves a model's
--   licence (okuro.ai_models.licensing.commercial_status) and stores the verdict
--   here so the Models "Discover" list and Studio can show whether a model is
--   safe to use in a commercial product BEFORE it's pulled or run. Conservative:
--   an unrecognised licence is 'unknown' (verify), never a false 'commercial'.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, idempotent. commercial_status ∈ commercial|conditional|
-- non_commercial|unknown. commercial_allowed is the gate decision (1/0).

ALTER TABLE model_discoveries ADD COLUMN commercial_status TEXT;
ALTER TABLE model_discoveries ADD COLUMN commercial_allowed INTEGER;
ALTER TABLE model_discoveries ADD COLUMN license_id TEXT;
