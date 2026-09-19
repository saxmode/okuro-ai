-- <!-- AGENT_HEADER
-- role: code
-- purpose: 151_model_consumers — the code-derived answer to "who uses this
--   model" (P2 of okuro-model-manager). Migration 150 gave okuro the model
--   UNITS on this host's stores; it could not say which tool reads which one,
--   and nothing else on the box could either — tm-inference's registry has no
--   consumer field, GASO's `purpose` is transient free text, and ComfyUI's
--   extra_model_paths maps buckets to PATHS, never to tools. One row here is
--   one model reference found at one `file:line` in one consumer's config.
-- index: content
-- AGENT_HEADER_END -->
--
-- Additive, idempotent. Nothing here is populated by the migration itself:
-- rows arrive from okuro.ai_models.consumers, which is a no-op unless the host
-- declares `ai_models.consumer_roots` in ~/.okuro/config.yaml. An okuro with no
-- declared consumers keeps the table empty and loses no function.
--
-- WHY A REFERENCE TABLE AND NOT A consumer↔unit JOIN TABLE. The useful row is
-- the CITATION, not the edge. "tm-mitate uses Llama-3.3-70B" is an assertion
-- someone has to go and verify; "tm-mitate names it at lib/lifecycle.py:50" is
-- the verification. A swap plan (P5) has to rewrite that exact line, so the
-- line number is load-bearing data, not provenance decoration.
--
-- WHY unit_id IS NULLABLE, AND WHY THAT IS THE POINT. A reference that resolves
-- to no unit is a DEAD REF — a consumer configured to load a file that is not
-- on this host. Two are known and ruled unresolved until the tm-mitate swap
-- work (rulings addendum, ruling 1): Llama-3.3-80B-Instruct.Q4_K_M.gguf and
-- Huihui-Qwen3-32B-abliterated-v2.i1-Q6_K.gguf. Dropping unresolved rows would
-- delete exactly the finding this phase exists to surface, so they are kept
-- with unit_id NULL and note 'dead-ref'. FOREIGN KEY is deliberately absent:
-- a re-scan of the stores may mark a unit missing and delete it, and that must
-- not cascade away the evidence of who was pointing at it.
--
-- WHY reachable IS SEPARATE FROM unit_id. A unit can exist on the host and
-- still be invisible to the consumer that names it. Measured shape on this
-- host: a GGUF under the HOT store is a symlink whose target lives on the COLD
-- store, which resolves perfectly for a shell, while the llama-cpp container
-- bind-mounts the hot store alone — so the file opens for a person and does
-- not exist for the process that wants it. `target_resolves`
-- in migration 150 answers "does this link go anywhere"; `reachable` answers
-- the different question "can THIS consumer get at it", and only the consumer's
-- mount can answer that.
--
-- WHY tier AND state ARE TWO COLUMNS. tier is policy and is stable — PROTECTED
-- is a standing ruling that a model must never be moved or deleted, and it does
-- not lapse because a service happens to be stopped this afternoon. state is
-- the measurement at scan time. Collapsing them would let a restart change a
-- protection decision, which is how a protected model gets proposed for
-- deletion.

CREATE TABLE IF NOT EXISTS model_consumers (
    consumer    TEXT NOT NULL,                    -- 'tm-mitate', 'comfyui-workflows', …
    run_mode    TEXT NOT NULL DEFAULT 'python'
                CHECK (run_mode IN ('systemd','compose','workflows','python','yaml','registry')),
    config_path TEXT NOT NULL,                    -- absolute path of the file that names the model
    line        INTEGER NOT NULL DEFAULT 0,       -- 1-based; 0 only for a whole-file fact
    model_ref   TEXT NOT NULL,                    -- the reference AS WRITTEN, before any rewriting
    unit_id     TEXT,                             -- model_units.unit_id, or NULL for a dead ref
    match_kind  TEXT,                             -- how it resolved: abs-path|path-suffix|filename|hf-name|alias|dir
    tier        TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (tier IN ('PROTECTED','ACTIVE','ARCHIVE')),
    state       TEXT NOT NULL DEFAULT 'configured'
                CHECK (state IN ('running','configured','stopped','script','unknown')),
    reachable   INTEGER NOT NULL DEFAULT 1,       -- 0 = this consumer's mount cannot see the unit
    note        TEXT,                             -- 'dead-ref', or why it is unreachable
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (consumer, config_path, line, model_ref)
);

CREATE INDEX IF NOT EXISTS idx_model_consumers_unit  ON model_consumers(unit_id);
CREATE INDEX IF NOT EXISTS idx_model_consumers_name  ON model_consumers(consumer);
CREATE INDEX IF NOT EXISTS idx_model_consumers_tier  ON model_consumers(tier);
CREATE INDEX IF NOT EXISTS idx_model_consumers_dead  ON model_consumers(unit_id, reachable);

-- ROLLBACK (statement, not a file — this repo has no rollback files):
--   DROP INDEX IF EXISTS idx_model_consumers_dead;
--   DROP INDEX IF EXISTS idx_model_consumers_tier;
--   DROP INDEX IF EXISTS idx_model_consumers_name;
--   DROP INDEX IF EXISTS idx_model_consumers_unit;
--   DROP TABLE IF EXISTS model_consumers;
