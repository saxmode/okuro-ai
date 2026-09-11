-- <!-- AGENT_HEADER
-- role: code
-- purpose: 113_interaction_analysis — session id bridge, human-turn friction markers, findings, trace lifecycle tiers.
-- index: content
-- AGENT_HEADER_END -->
--
-- Closes Meta-Harness GAP 1 (P1 full-trace access) on the HUMAN side.
--
-- Context. okuro carries two session-id namespaces for the same conversation:
--   sessions.session_id        okuro's own telemetry uuid, minted at bootstrap
--   agent_sessions.session_id  the provider's native id (the JSONL filename)
-- They join on exactly 1 row out of 9369/10383. `sense/retros.py:_find_cohorts`
-- documents this and deliberately skips the join, so every retro to date ran on
-- score + tool-list metadata with zero transcript access — the "scores-only"
-- ablation the Meta-Harness paper measures as 15+ points worse than full-trace.
--
-- `sessions.provider_session_id` was added later and holds the native id, but it
-- is only populated from 2026-07 onward (2119 of 10383 rows; zero for Apr-Jun).
-- `session_bridge` generalizes it: declared links where the column is set,
-- heuristic links (provider + project_path + start-time proximity) everywhere
-- else, each carrying a confidence so analysis can filter on match quality
-- rather than trusting every pairing equally.
--
-- Storage note. Nothing here duplicates transcript text. Human turns stay in
-- agent_events (they are 2.8% of the corpus and are never compacted); these
-- tables hold only the join, the derived markers, the findings, and the tier
-- state. `vec_interaction_turns` is created post-migrate by ensure_vec_dims()
-- at the active embedding tier dim, same as every other vec_* table.

-- ---------------------------------------------------------------------------
-- 1. Session id bridge
-- ---------------------------------------------------------------------------

-- Telemetry uuid -> native trace id. Many telemetry sessions legitimately map
-- to ONE native id: Claude Code resumes a transcript, so a single JSONL file
-- accumulates across several okuro sessions. Hence the PK is the telemetry
-- side, and native_session_id is deliberately non-unique.
CREATE TABLE IF NOT EXISTS session_bridge (
    telemetry_session_id TEXT PRIMARY KEY,          -- sessions.session_id
    native_session_id    TEXT NOT NULL,             -- agent_sessions.session_id
    method               TEXT NOT NULL              -- how the pair was established
                         CHECK (method IN ('declared', 'embedded')),
    confidence           REAL NOT NULL DEFAULT 1.0, -- both methods exact; see bridge.py
    matched_on           TEXT NOT NULL DEFAULT '{}',-- JSON: signals used
    created_at           TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_bridge_native
    ON session_bridge(native_session_id);
CREATE INDEX IF NOT EXISTS idx_session_bridge_confidence
    ON session_bridge(confidence DESC);

-- Subagent -> spawning parent. A DIFFERENT relation from the one above and a
-- different cardinality: one parent spawns many subagents, so the child is the
-- key. Folding this into session_bridge collapsed 4838 subagent links down to
-- 473, because every sibling collided on the shared parent id.
--
-- Read straight off the transcript path, which Claude Code shapes as
-- .../projects/<slug>/<parent-native-id>/subagents/<agent-id>.jsonl — so the
-- link is structural, not inferred.
CREATE TABLE IF NOT EXISTS session_parents (
    child_native_id   TEXT PRIMARY KEY,
    parent_native_id  TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'transcript_path',
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_parents_parent
    ON session_parents(parent_native_id);

-- ---------------------------------------------------------------------------
-- 2. Friction markers on human turns
-- ---------------------------------------------------------------------------
-- One row per (human turn, detector that fired). Derived data — safe to drop
-- and recompute from agent_events at any time. Keyed on the native event uuid
-- so it survives re-ingest (agent_events.uuid is the provider's own id).

CREATE TABLE IF NOT EXISTS interaction_markers (
    event_uuid        TEXT NOT NULL,     -- agent_events.uuid (a type='user' row)
    native_session_id TEXT NOT NULL,
    marker            TEXT NOT NULL,     -- detector id, e.g. 'correction'
    weight            REAL NOT NULL DEFAULT 1.0,
    evidence          TEXT,              -- the matched span, capped
    turn_index        INTEGER,           -- nth human turn within the session
    ts                TEXT,
    detected_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (event_uuid, marker)
);

CREATE INDEX IF NOT EXISTS idx_interaction_markers_session
    ON interaction_markers(native_session_id);
CREATE INDEX IF NOT EXISTS idx_interaction_markers_marker
    ON interaction_markers(marker, ts);

-- Sessions whose human turns have already been scanned. Without this the
-- detector re-walks the whole corpus every run; markers are sparse so their
-- absence cannot distinguish "clean session" from "not yet scanned".
CREATE TABLE IF NOT EXISTS interaction_scanned (
    native_session_id TEXT PRIMARY KEY,
    turns_scanned     INTEGER NOT NULL DEFAULT 0,
    markers_found     INTEGER NOT NULL DEFAULT 0,
    detector_version  INTEGER NOT NULL DEFAULT 1,
    scanned_at        TEXT DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- 3. Findings
-- ---------------------------------------------------------------------------
-- The durable output of the pipeline. Survives compaction of the traces that
-- produced it — this is what makes discarding raw events acceptable later.

CREATE TABLE IF NOT EXISTS interaction_findings (
    id              TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    name            TEXT NOT NULL,        -- snake_case pattern id
    description     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'medium'
                    CHECK (severity IN ('low', 'medium', 'high')),
    markers         TEXT NOT NULL DEFAULT '[]',  -- JSON: detector ids implicated
    evidence        TEXT NOT NULL DEFAULT '[]',  -- JSON: event uuids / session ids
    session_count   INTEGER NOT NULL DEFAULT 0,
    recommendation  TEXT,
    memory_id       TEXT,                 -- gotcha memory written, if any
    window_start    TEXT,
    window_end      TEXT,
    model           TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interaction_findings_batch
    ON interaction_findings(batch_id);
CREATE INDEX IF NOT EXISTS idx_interaction_findings_name
    ON interaction_findings(name, created_at DESC);

CREATE TABLE IF NOT EXISTS interaction_batches (
    batch_id        TEXT PRIMARY KEY,
    ran_at          TEXT DEFAULT (datetime('now')),
    window_start    TEXT,
    window_end      TEXT,
    sessions_in     INTEGER NOT NULL DEFAULT 0,
    turns_in        INTEGER NOT NULL DEFAULT 0,
    findings_out    INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    error           TEXT
);

-- ---------------------------------------------------------------------------
-- 4. Trace lifecycle tiers
-- ---------------------------------------------------------------------------
-- HOT   0-30d    everything verbatim
-- WARM  30-90d   everything verbatim; analysis runs in this band
-- COOL  90-180d  tool_result bodies dropped
-- COLD  >180d    only human turns + session metrics retained
--
-- Human turns (type='user' in a non-subagent session) are NEVER compacted at
-- any tier — they are 2.8% of the corpus and 100% of the signal this pipeline
-- exists to read. Nothing is ever deleted outright: compaction nulls bodies and
-- records what it reclaimed, so the event skeleton (timing, DAG, tool names,
-- token counts) survives forever.

CREATE TABLE IF NOT EXISTS trace_lifecycle (
    native_session_id TEXT PRIMARY KEY,
    tier              TEXT NOT NULL DEFAULT 'hot'
                      CHECK (tier IN ('hot', 'warm', 'cool', 'cold')),
    analyzed_at       TEXT,               -- set once the session fed a batch
    analyzed_batch    TEXT,
    compacted_at      TEXT,
    bytes_before      INTEGER,
    bytes_reclaimed   INTEGER NOT NULL DEFAULT 0,
    events_nulled     INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trace_lifecycle_tier
    ON trace_lifecycle(tier);
CREATE INDEX IF NOT EXISTS idx_trace_lifecycle_unanalyzed
    ON trace_lifecycle(analyzed_at) WHERE analyzed_at IS NULL;

-- vec_interaction_turns created post-migrate by ensure_vec_dims() at active dim.
