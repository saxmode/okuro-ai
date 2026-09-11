-- <!-- AGENT_HEADER
-- role: code
-- purpose: 084_model_discoveries — durable store for the weekly model-scan's
--   fit-gated candidates. The scan (okuro.ai_models.suggest:run_suggestions,
--   Mon 04:00) already files each pick as a `proactive` signal for the
--   notification surface (Now page / morning brief); this table is the
--   companion LIST the /models "Discover" tab reads by default, so a discovery
--   survives the user dismissing its notification. Signal = notify; this = list.
--   One row per catalog_id (e.g. huggingface:owner/repo). status carries the
--   lifecycle: new → acknowledged → installed, or dismissed (hidden). Rows are
--   upserted on every scan so last_seen_at/score refresh without losing status.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, idempotent. evidence holds the full ModelSuggestion.to_evidence()
-- JSON so future columns can be derived without a re-scan.

CREATE TABLE IF NOT EXISTS model_discoveries (
    catalog_id    TEXT PRIMARY KEY,                     -- huggingface:owner/repo | civitai:...
    display_name  TEXT NOT NULL,
    modality      TEXT NOT NULL DEFAULT 'text',          -- text|image|audio|...
    min_vram_gb   REAL,
    fit_gpu       TEXT,                                  -- named GPU it fits, e.g. 'GPU1 24GB'
    score         REAL,                                  -- catalog.score_quality at scan time
    rationale     TEXT,                                  -- why it's worth running for THIS user
    use_case      TEXT,
    caveats       TEXT,
    source_url    TEXT,
    researched    INTEGER NOT NULL DEFAULT 0,            -- 1 = LLM rationale, 0 = deterministic
    status        TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'acknowledged', 'installed', 'dismissed')),
    evidence      TEXT NOT NULL DEFAULT '{}',            -- full to_evidence() JSON
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_model_disc_status ON model_discoveries(status);
CREATE INDEX IF NOT EXISTS idx_model_disc_score  ON model_discoveries(score DESC);
CREATE INDEX IF NOT EXISTS idx_model_disc_seen   ON model_discoveries(last_seen_at DESC);
