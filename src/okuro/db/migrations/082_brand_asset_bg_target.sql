-- 082_brand_asset_bg_target — which background a brand asset (chiefly a logo)
-- is meant to sit on. Lets a brand carry a light-bg logo AND a dark-bg logo,
-- or a single logo that works on 'both'. The prism/slides renderer auto-picks
-- the right variant by measuring the canvas background luminance.
--
-- bg_target:
--   both   — works on any background (default; the single-logo case)
--   light  — designed for LIGHT backgrounds (usually a dark/coloured mark)
--   dark   — designed for DARK backgrounds (usually a light/white mark)
--
-- Constant default keeps ADD COLUMN safe; existing rows backfill to 'both'.

ALTER TABLE brand_assets ADD COLUMN bg_target TEXT NOT NULL DEFAULT 'both'
    CHECK (bg_target IN ('dark', 'light', 'both'));
