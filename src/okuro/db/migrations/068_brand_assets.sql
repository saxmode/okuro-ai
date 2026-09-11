-- <!-- AGENT_HEADER
-- role: code
-- purpose: 068_brand_assets — uploaded binary brand assets (logos, images,
--   portraits, signatures, backgrounds) scoped to a brand. Distinct from
--   asset_profiles (063), which is the ICON-provider registry, not uploads.
-- index: content
-- AGENT_HEADER_END -->
--
-- A general brand-asset library reused by okuro-slides (and okuro-video next):
-- the user uploads brand imagery once, tagged to a brand + a kind, then drops
-- it into decks/videos. Bytes live on disk under ~/.okuro/assets/<brand>/<id>.<ext>
-- (images can be large); only metadata + the path are stored here. Served via
-- /api/brand-assets/{id}/raw.

CREATE TABLE IF NOT EXISTS brand_assets (
    id          TEXT PRIMARY KEY,                         -- uuid hex
    brand_id    TEXT NOT NULL DEFAULT 'default',
    kind        TEXT NOT NULL DEFAULT 'image'
                CHECK (kind IN ('logo', 'image', 'portrait', 'signature', 'background', 'other')),
    name        TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT 'application/octet-stream',
    path        TEXT NOT NULL,                            -- absolute file path on disk
    width       INTEGER,
    height      INTEGER,
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_brand_assets_brand ON brand_assets(brand_id, kind, created_at DESC);
