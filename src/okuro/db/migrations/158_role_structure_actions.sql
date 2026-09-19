-- <!-- AGENT_HEADER
-- role: code
-- purpose: The action container — one row per structural change under
--   consideration, its seven-state machine, the append-only event log that
--   records every move, and the cross-process claim that stops two uvicorn
--   workers dispatching the same research run.
-- index: content
-- AGENT_HEADER_END -->
--
-- THE CLASS THIS FIXES: A FINDING WITH NOWHERE TO GO.
--
-- Migration 157 made a finding CHECKABLE — the body is stored, the quote is a
-- substring assert rather than a judgement. What it did not do is give the
-- finding a life after the report was written. A report is read once, and an
-- artifact nobody turned into work is indistinguishable from a finding nobody
-- had. Three months later the only evidence that the fleet was told about a
-- spec change is a paragraph in a document.
--
-- So a finding becomes a ROW, and the row has a state. The state machine is
-- the owner's own sequence and it is not decoration: each arrow names who may
-- move it and what has to exist before it moves.
--
--     proposed → researched → planned → critiqued → approved
--              → implemented → verified
--
-- with `rejected` and `superseded` as terminal exits from anywhere.
--
-- ------------------------------------------------------------------------
-- WHY THE HUMAN GATE IS ONE ARROW AND NOT A FLAG.
--
-- `→ approved` is the only transition an agent cannot make. That is enforced
-- three times over, deliberately, because one fence is one bug away from
-- nothing: the MCP verb does not accept the state, the API route that does
-- sits behind the loopback + bearer gate, and `roles.actions.transition`
-- refuses an actor that is not the profile's own human. An agent that reaches
-- any one of those has still not reached the other two.
--
-- Reaching `approved` emits exactly ONE todo, through `sense.todos.todo_add`
-- rather than through SQL written here, so the owner's personal queue gains one
-- line per approved item and the fleet-wide backlog stays in this table. That
-- is Q3 as ruled on 2026-09-17: a new table that EMITS a todo, not a todo
-- table pressed into being a state machine.
--
-- ------------------------------------------------------------------------
-- WHY `affected_role_ids` IS A JSON LIST AND NEVER A COUNT.
--
-- "34 roles affected" is the shape of claim the whole workstream exists to
-- stop. A count cannot be checked, cannot be diffed after the change, and
-- cannot be wrong in a way anybody notices. The explicit list can be all
-- three. Design plan Decision 3 says "explicit list, never a count" and this
-- column is where that is either true or a lie.
--
-- ------------------------------------------------------------------------
-- WHY `rubric_version` AND `source_hash_at_proposal` ARE COLUMNS.
--
-- Amendment A5, both halves:
--
--   * `rubric_version` — a fit score is only comparable to another score taken
--     under the same rubric. `→ verified` asks "did the affected roles'
--     structure score go down", and that question is meaningless across a
--     rubric change. Storing the version the action was proposed under makes
--     the comparison refuse rather than quietly compare two different rulers.
--
--   * `source_hash_at_proposal` — an action proposed against a sentence that
--     the source has since replaced is an action about a page that no longer
--     exists. `→ approved` refuses on drift and drops the row back to
--     `researched`, with an event saying so. The alternative is approving a
--     change to okuro's canon on the strength of a quote nobody can find any
--     more.
--
-- AND THE HASH IS NEVER REWRITTEN BY THE REFUSAL. A drop-back that quietly
-- re-anchored the row to the new hash would make the refusal a formality: the
-- plan and critique artifacts are still on the row, so it walks straight back
-- to `critiqued` and approves against evidence nobody re-read. Re-anchoring is
-- its own transition (`researched → researched`), it re-verifies the quote
-- against the new body, and it CLEARS the plan and critique ids so the re-work
-- is real work rather than three clicks.
--
-- ------------------------------------------------------------------------
-- WHY THERE IS A THIRD TABLE (role_structure_dispatch_claims).
--
-- P3 left the structure-research dispatch guarded by a Python `set` and a scan
-- of task directories, and its own report named the hole: the set is per
-- process, so two uvicorn workers receiving simultaneous clicks both pass it,
-- and the directory scan cannot see a run that has not spawned yet. Both
-- agents then get handed the same nine sources.
--
-- A cross-process claim needs a row, and it needs the uniqueness to be the
-- database's rather than the caller's — an INSERT that conflicts is atomic,
-- a SELECT-then-INSERT is the race it was meant to close. The registry state
-- is the primary key because that is what the guard was always keyed on: two
-- dispatches against the same state are duplicate work, and a dispatch after
-- a source moved is a different run that must never wait.

-- ── The action container ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS role_structure_actions (
    id                      TEXT PRIMARY KEY,
    -- `finding` comes from the researcher's report: something outside okuro
    -- changed and one named okuro element is implicated.
    -- `source_health` comes from the poller: a registered source stopped
    -- behaving, which is not a finding about roles but still needs a human.
    -- Both live here so a dead feed is visible in the same list as the work.
    kind                    TEXT NOT NULL
                            CHECK (kind IN ('finding', 'source_health')),
    state                   TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (state IN (
                                'proposed', 'researched', 'planned',
                                'critiqued', 'approved', 'implemented',
                                'verified', 'rejected', 'superseded')),
    title                   TEXT NOT NULL,

    -- ── Provenance. For a finding these are the evidence; for a
    -- source_health row they name the source that misbehaved.
    source_id               TEXT
                            REFERENCES role_structure_sources(id)
                            ON DELETE SET NULL,
    run_id                  TEXT,
    -- The exact blob the quote was checked against. Resolved at propose time
    -- from the run's observation row, never supplied by the caller: a caller
    -- that picks its own fetch_id picks which body it is judged against.
    fetch_id                TEXT
                            REFERENCES source_fetches(id) ON DELETE SET NULL,
    evidence_url            TEXT,
    quoted_sentence         TEXT,
    -- Always the RETURN VALUE of verify_quote, never a claim. A row where
    -- this is 0 and kind is 'finding' cannot exist: propose refuses first.
    quote_verified          INTEGER DEFAULT 0,

    -- ── What it bears on.
    okuro_element           TEXT,
    -- JSON array of role_id strings. EXPLICIT LIST, NEVER A COUNT — see the
    -- header. Empty array, never NULL, so "none yet" and "not recorded" do
    -- not read identically.
    affected_role_ids       TEXT DEFAULT '[]',

    -- ── The two fixed reference points (amendment A5).
    rubric_version          TEXT,
    source_hash_at_proposal TEXT,

    -- ── One artifact per phase. The state machine requires the matching id
    -- to exist before it will make the move, which is what stops a state
    -- from being a label somebody typed.
    research_artifact_id    TEXT,
    plan_artifact_id        TEXT,
    critique_artifact_id    TEXT,
    diff_artifact_id        TEXT,
    migration_id            TEXT,

    -- The one todo this action emits when it reaches `approved`. Set once.
    todo_id                 TEXT,

    -- WHY THIS ROW IS WHERE IT IS, when where it is was not somebody's choice.
    --
    -- A refused approval on hash drift does not raise; it moves the row back to
    -- `researched`. Without this column that move is indistinguishable from a
    -- deliberate one, and the panel shows a row sitting in `researched` for a
    -- reason only the event log holds. Written on every refusal that MOVES the
    -- row, cleared by the re-anchor that resolves it.
    last_refusal            TEXT,

    created_by              TEXT,
    created_at              TEXT DEFAULT (datetime('now')),
    -- Who approved or rejected it, and when. Only those two transitions
    -- write here: they are the decisions, the rest are progress.
    decided_by              TEXT,
    decided_at              TEXT,
    superseded_by           TEXT
                            REFERENCES role_structure_actions(id)
                            ON DELETE SET NULL,
    updated_at              TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_role_structure_actions_state
    ON role_structure_actions(state);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_kind
    ON role_structure_actions(kind, state);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_source
    ON role_structure_actions(source_id);
CREATE INDEX IF NOT EXISTS idx_role_structure_actions_run
    ON role_structure_actions(run_id);

-- ── The source_health dedupe, enforced by the database ───────────────────
--
-- The poller runs weekly and an alarm persists as long as its cause does.
-- `arxiv-persona-prompting` alarms on EVERY run and will keep doing so until
-- arXiv serves uncached queries again — a correct alarm, and one that would
-- mint 52 identical rows a year with nothing to stop it.
--
-- So: at most one OPEN source_health row per (source, title), where the title
-- names the alarm. Closed rows (verified / rejected / superseded) are not in
-- the index, so an alarm that returns after being dealt with opens a new row
-- rather than silently reusing the resolved one.
--
-- A partial UNIQUE index rather than a check in Python, because the check in
-- Python is a SELECT followed by an INSERT and two pollers fit between them.

CREATE UNIQUE INDEX IF NOT EXISTS idx_role_structure_actions_open_health
    ON role_structure_actions(source_id, title)
    WHERE kind = 'source_health'
      AND state NOT IN ('verified', 'rejected', 'superseded');

-- ── The event log ────────────────────────────────────────────────────────
--
-- Append-only. Every transition writes one row INCLUDING the refusals that
-- move a state backwards — an approval that failed the hash check leaves a
-- `critiqued → researched` event naming the drift, so "why is this back in
-- research" has an answer that is not somebody's memory.
--
-- `from_state` is NULL for the birth event, which is how a row's first
-- appearance is told apart from a move into `proposed` (there is no such
-- move: `proposed` is only ever an origin).

CREATE TABLE IF NOT EXISTS role_structure_action_events (
    id         TEXT PRIMARY KEY,
    action_id  TEXT NOT NULL
               REFERENCES role_structure_actions(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor      TEXT,
    reason     TEXT,
    at         TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_role_structure_action_events_action
    ON role_structure_action_events(action_id, at);

-- ── The dispatch claim ───────────────────────────────────────────────────
--
-- One row per registry state that a structure-research dispatch has claimed.
-- `released_at` NULL means in flight. The PRIMARY KEY is what makes the claim
-- atomic across processes; `INSERT … ON CONFLICT DO NOTHING` either claims it
-- or reports that somebody else holds it, in one statement.
--
-- `claimed_at` exists so a claim orphaned by a crashed worker can expire.
-- Without it the first crash during a poll would lock that registry state
-- out forever, and the repair would be a human deleting a row they have no
-- reason to know about.

CREATE TABLE IF NOT EXISTS role_structure_dispatch_claims (
    registry_state TEXT PRIMARY KEY,
    run_id         TEXT,
    task_id        TEXT,
    claimed_at     TEXT DEFAULT (datetime('now')),
    released_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_role_structure_dispatch_claims_open
    ON role_structure_dispatch_claims(released_at);
