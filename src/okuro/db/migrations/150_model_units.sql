-- <!-- AGENT_HEADER
-- role: code
-- purpose: 150_model_units — store-aware inventory of the model UNITS that live
--   on this host's configured model stores (P1 of okuro-model-manager). Until
--   now okuro knew only its OWN bundle store under ~/.okuro/models/bundles and
--   the `model_discoveries` candidate list; the terabytes actually sitting on
--   the host's hot and cold model stores were invisible to it.
--   `model_units` is one row per UNIT — the thing a human calls "a model" —
--   and `model_placements` is where that unit's bytes physically are, including
--   the symlink facts that decide whether a consumer can actually reach it.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, idempotent. Nothing here is populated by the migration itself:
-- rows arrive from okuro.ai_models.store_scan, which is a no-op unless the
-- host declares `ai_models.model_stores` in ~/.okuro/config.yaml. An okuro
-- with no local model stores keeps both tables empty and loses no function —
-- okuro never depends on local inference.
--
-- WHY A UNIT TABLE AND NOT A FILE TABLE. The two stores hold 582,277 files.
-- A file table would be a database of shards, blobs and tokenizer json that
-- nobody reasons about; the store audits that preceded this phase all worked
-- at unit granularity (an HF cache dir, a leaf model dir, or one standalone
-- .gguf in a shared bucket) because that is the granularity at which a model
-- is kept, moved, or deleted. The scanner's unit rules live in store_scan.py.
--
-- WHY size_bytes AND alloc_bytes. A failed HuggingFace download leaves a
-- SPARSE .incomplete blob: `find -printf %s` reports 11.78 GB of apparent size
-- for partials that occupy 6.56 GB of actual disk. Reporting only one of the
-- two either overstates what deleting would reclaim or understates what a
-- store walk should sum to. Both are cheap (one os.stat gives both), so both
-- are recorded and the caller picks.
--
-- DELIBERATELY ABSENT: family, version, variant_tags. Lineage is P3 and it
-- needs a matcher, not a column — adding the columns early would invite rows
-- to be filled in by hand and then trusted.

CREATE TABLE IF NOT EXISTS model_units (
    unit_id     TEXT PRIMARY KEY,                 -- "<store>:<rel_path>", stable across scans
    name        TEXT NOT NULL,                    -- human name (dir name, shard stem, or filename)
    store       TEXT NOT NULL,                    -- configured store name, e.g. 'nvme' | 'raid'
    rel_path    TEXT NOT NULL,                    -- path relative to the store root
    size_bytes  INTEGER NOT NULL DEFAULT 0,       -- apparent bytes (sum of st_size)
    alloc_bytes INTEGER NOT NULL DEFAULT 0,       -- allocated bytes (sum of st_blocks*512)
    file_count  INTEGER NOT NULL DEFAULT 0,
    format      TEXT NOT NULL DEFAULT 'other'
                CHECK (format IN ('gguf','safetensors','bin','pth','onnx','ckpt','other','mixed')),
    layout      TEXT NOT NULL DEFAULT 'file'
                CHECK (layout IN ('hf-cache','flat-dir','file')),
    identity    TEXT,                             -- "<size>:<sha256(head1MiB+tail1MiB)[:32]>", NULL until identity mode runs
    status      TEXT NOT NULL DEFAULT 'ok'
                CHECK (status IN ('ok','broken','missing')),
    note        TEXT,                             -- why it is broken/missing, in one line
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (store, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_model_units_store   ON model_units(store, size_bytes DESC);
CREATE INDEX IF NOT EXISTS idx_model_units_ident   ON model_units(identity);
CREATE INDEX IF NOT EXISTS idx_model_units_status  ON model_units(status);
CREATE INDEX IF NOT EXISTS idx_model_units_format  ON model_units(format);

-- One row per physical location of a unit. Today a unit has exactly one
-- placement — the path it was scanned at — but the column set exists because
-- the interesting failure is a placement that LOOKS fine and is not. Two
-- shapes, both measured on this host's stores: a symlink whose target is
-- written the way a CONTAINER sees it, which resolves nowhere on the host at
-- all; and a symlink that resolves perfectly on the host into the cold store
-- while the container that wants the file mounts only the hot store, so the
-- consumer cannot reach it. target_resolves answers the first question; the
-- second needs the consumer mount map and is P2.
CREATE TABLE IF NOT EXISTS model_placements (
    unit_id         TEXT NOT NULL REFERENCES model_units(unit_id) ON DELETE CASCADE,
    store           TEXT NOT NULL,
    abs_path        TEXT NOT NULL,
    is_symlink      INTEGER NOT NULL DEFAULT 0,
    link_target     TEXT,                          -- readlink() value, verbatim (may be container-relative)
    target_resolves INTEGER NOT NULL DEFAULT 1,    -- 0 = dangling on this host
    PRIMARY KEY (unit_id, abs_path)
);

CREATE INDEX IF NOT EXISTS idx_model_placements_unit ON model_placements(unit_id);
CREATE INDEX IF NOT EXISTS idx_model_placements_link ON model_placements(target_resolves);

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP INDEX IF EXISTS idx_model_placements_link;
--   DROP INDEX IF EXISTS idx_model_placements_unit;
--   DROP TABLE IF EXISTS model_placements;
--   DROP INDEX IF EXISTS idx_model_units_format;
--   DROP INDEX IF EXISTS idx_model_units_status;
--   DROP INDEX IF EXISTS idx_model_units_ident;
--   DROP INDEX IF EXISTS idx_model_units_store;
--   DROP TABLE IF EXISTS model_units;
