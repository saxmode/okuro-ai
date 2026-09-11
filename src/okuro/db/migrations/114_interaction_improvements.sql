-- <!-- AGENT_HEADER
-- role: code
-- purpose: 114_interaction_improvements — proposals routed to a mutable surface, with a baseline so effectiveness is measurable.
-- index: content
-- AGENT_HEADER_END -->
--
-- Stage 3 of the interaction pipeline. `interaction_findings` records WHAT is
-- wrong; this records WHAT TO CHANGE, against which surface, and — critically —
-- what the problem rate was when the proposal was made.
--
-- Why the baseline column exists. Measured over eight months, frustration
-- markers do not trend down (318 in 2026-02, 181, 171, 88, 172, 107 in
-- 2026-07). Findings alone cannot tell you whether anything got better, so a
-- system that only proposes accumulates advice rather than improvement. Every
-- proposal therefore stamps the marker rate at proposal time and is re-measured
-- after promotion; one that does not move its metric is flagged ineffective
-- rather than quietly assumed to have worked.
--
-- Proposals are emitted into the existing `signals` queue (source='proactive'),
-- which already carries open -> promoted/discarded and a proposals API in the
-- web UI. No parallel approval surface is introduced — `signal_id` below is the
-- join back to it.

CREATE TABLE IF NOT EXISTS interaction_improvements (
    id                TEXT PRIMARY KEY,
    batch_id          TEXT,
    finding_id        TEXT REFERENCES interaction_findings(id),
    signal_id         TEXT,               -- signals.id; the approval surface

    surface           TEXT NOT NULL       -- what okuro would change
                      CHECK (surface IN (
                          'subagent_brief',    -- spawn template + pre-spawn gate
                          'enforcement_hook',  -- pre/post tool gates
                          'role',              -- role body / role_knowledge
                          'user_profile',      -- behavioral contract
                          'principle',         -- DP/SYS/ORCH principle set
                          'bootstrap',         -- packet composition
                          'memory_hygiene'     -- decay, dedupe, acknowledge
                      )),
    target_ref        TEXT,               -- role_id, principle id, section name
    title             TEXT NOT NULL,
    proposal          TEXT NOT NULL,      -- the concrete change to make
    rationale         TEXT,

    -- Effectiveness measurement
    markers           TEXT NOT NULL DEFAULT '[]',  -- JSON: marker ids tracked
    baseline_value    REAL,               -- marker rate per session at proposal
    baseline_window   TEXT,               -- e.g. '30d'
    baseline_at       TEXT DEFAULT (datetime('now')),
    verify_after      TEXT,               -- earliest date to re-measure
    verified_at       TEXT,
    verified_value    REAL,
    outcome           TEXT                -- NULL until verified
                      CHECK (outcome IS NULL OR outcome IN (
                          'confirmed',    -- metric moved the right way
                          'ineffective',  -- promoted but metric did not move
                          'inconclusive', -- too little data to judge
                          'not_promoted'  -- user discarded it
                      )),

    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'promoted', 'discarded', 'verified')),
    model             TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interaction_improvements_surface
    ON interaction_improvements(surface, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_interaction_improvements_status
    ON interaction_improvements(status);
CREATE INDEX IF NOT EXISTS idx_interaction_improvements_signal
    ON interaction_improvements(signal_id);
-- Due for verification: promoted, past its wait, not yet judged.
CREATE INDEX IF NOT EXISTS idx_interaction_improvements_due
    ON interaction_improvements(verify_after)
    WHERE verified_at IS NULL;

-- Which role was active in a session. Needed to route friction markers to the
-- role that was driving when the human pushed back — without it, "improve the
-- roles" has no addressable target.
--
-- Two sources, both read from the transcript: an explicit roles_get(role_id)
-- tool call (960 sessions) and the bootstrap packet's own role assignment
-- (381 sessions).
CREATE TABLE IF NOT EXISTS session_roles (
    native_session_id TEXT NOT NULL,
    role_id           TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'roles_get'
                      CHECK (source IN ('roles_get', 'bootstrap')),
    ord               INTEGER,            -- event order, so first role is knowable
    detected_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (native_session_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_session_roles_role
    ON session_roles(role_id);
