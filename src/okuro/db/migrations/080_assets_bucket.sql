-- <!-- AGENT_HEADER
-- role: code
-- purpose: 080_assets_bucket — the unified media bucket. okuro "assets" becomes
--   the single filestore for ALL generated/imported media (image|video|audio|
--   icon|illustration), not just the licensed icon library. Studio (ComfyUI)
--   generations register here (source=studio, folder=studio-generations); the
--   Studio UI reads the same bucket filtered+flattened. Tagging is generalised
--   across every kind via asset_tags (mirrors the icon tags/sets machinery).
--   Bytes for large media stay on disk (referenced by abs_path/rel_path) — this
--   table is the INDEX, not a blob store (contrast note_images which blobs small
--   pastes). The licensed 35k-icon library.db is left untouched; icons remain
--   their own read-only provider and are unioned at the view layer.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, idempotent. rel_path = bucket-managed bytes under assets_root;
-- abs_path = a file referenced in place (studio generations keep living in
-- ~/.okuro/generations so the live /image/{name} route keeps working). Exactly
-- one of rel_path / abs_path is set per row.

CREATE TABLE IF NOT EXISTS assets (
    id         TEXT PRIMARY KEY,                       -- uuid4
    kind       TEXT NOT NULL,                          -- image|video|audio|icon|illustration
    source     TEXT NOT NULL,                          -- studio|upload|import
    folder     TEXT,                                   -- flat collection, e.g. 'studio-generations'
    rel_path   TEXT,                                   -- relative to assets_root (bucket-owned bytes)
    abs_path   TEXT,                                   -- absolute path (referenced-in-place file)
    mime       TEXT,
    width      INTEGER,
    height     INTEGER,
    byte_size  INTEGER,
    title      TEXT,
    meta       TEXT,                                   -- JSON: prompt, model, family, preset, seed, ...
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Dedupe: a referenced-in-place file registers exactly once (studio re-scan safe).
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_abspath
    ON assets(abs_path) WHERE abs_path IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_assets_kind    ON assets(kind);
CREATE INDEX IF NOT EXISTS idx_assets_source  ON assets(source);
CREATE INDEX IF NOT EXISTS idx_assets_folder  ON assets(folder);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at DESC);

CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id TEXT NOT NULL,
    tag      TEXT NOT NULL,                            -- lowercased
    PRIMARY KEY (asset_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag);
