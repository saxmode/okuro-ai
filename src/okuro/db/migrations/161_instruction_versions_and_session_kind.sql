-- <!-- AGENT_HEADER
-- role: code
-- purpose: The two facts a session's adoption rate needs and the store never
--   kept — which instruction version was live when it started, and whether a
--   human or an agent started it.
-- index: content
-- AGENT_HEADER_END -->
--
-- THE CLASS THIS FIXES: THE TRACE STORE RECORDS A SESSION'S CONTENT AND NOT
-- ITS PROVENANCE.
--
-- An adoption rate is a fraction, and both halves of it are currently unsafe:
--
--   * The DENOMINATOR mixes instruction versions. okuro rewrites every
--     provider instruction file on the daemon's `*/5` refresh, and nothing
--     recorded which text was live when a given session started. A rate
--     computed across a change silently averages the before and the after,
--     and the number looks exactly as confident as a valid one.
--
--   * The NUMERATOR mixes actors. 110 of the newest 200 claude-code
--     transcripts under ~/.claude/projects carry `isSidechain: true`
--     (measured 2026-09-17) — they are subagents spawned by one interactive
--     session, each bootstrapping. 278 of 650 codex rollout files were
--     written by `codex_exec`, and 94 more by `Codex Desktop` importing
--     conversations that never ran on this machine at all. All of them count
--     as sessions today.
--
-- TWO PROBLEMS, TWO SHAPES — AND THAT IS WHY THIS MIGRATION DOES TWO THINGS.
--
-- Instruction version is an AMBIENT WORLD-FACT: it is true of a moment, not
-- of a session, and it would be true if no session had ever run. A timeline
-- is the shape of that, and every reader derives from it at query time. No
-- session row has to carry it, nothing has to be backfilled, and a row added
-- to the timeline today makes every past query more correct rather than
-- leaving old rows stale.
--
-- Who started a session is a PER-ARTIFACT FACT: it is true of that one
-- transcript and is legible only while the transcript is open. codex writes
-- it in `session_meta.originator`, which ingest discards; nothing left in the
-- store can reconstruct it. That one has to be a column.
--
-- (An earlier draft justified the split by "ingesters are unreliable, so
-- derive". That is not a rule — it would argue against the column too, and
-- the column is right. Ambient-vs-per-artifact is the real discriminator.)
--
-- ------------------------------------------------------------------------
-- A SESSION IS NOT A MOMENT, SO IT CANNOT SIMPLY BE STAMPED WITH ONE VERSION.
--
-- `agent_sessions.first_ts` is when the transcript FILE began, not when a run
-- began — Claude Code resumes sessions and one JSONL accumulates across days
-- (okuro measured this in sense/interaction/bridge.py: start-time delta up to
-- 210,543 s, mean 7.2 h; 1,930 okuro sessions collapse to 118 native ids).
-- Measured here 2026-09-17: 718 of 1,014 claude-code sessions in the last 30
-- days span more than the 5-minute refresh interval, and 35 span more than a
-- day.
--
-- So a session can straddle a version boundary, and stamping it with the
-- version live at `first_ts` would emit a confident digest for instructions
-- half its turns never saw. The reader therefore resolves the version at BOTH
-- ends of a session: equal means the whole session sat in one version;
-- different means it SPANNED, and a spanning session is counted in its own
-- bucket and never inside a cohort. Boundaries are totally ordered in time,
-- so equal-at-both-ends is proof that none was crossed — not a sample.
--
-- ------------------------------------------------------------------------
-- WHY THE LEDGER IS A TABLE AND NOT THE MANIFEST.
--
-- ~/.okuro/deployed-instructions.json (added by _deployment.py, 2026-09-17)
-- records the CURRENT digest of each okuro-written instruction file and the
-- time it was written. That is a snapshot, and a snapshot answers "what is
-- live now", never "what was live then". One entry per path, overwritten in
-- place: the moment the content changes, the previous version's existence is
-- gone. A join needs a timeline.
--
-- The manifest is still the ledger's seed. `record()` rewrites `written_at`
-- only when the digest actually changes (it early-returns on an unchanged
-- hash, so the `*/5` refresh does not restamp it 288 times a day) — which
-- makes `written_at` exactly "when this content became live". The ledger
-- reads it once, for the first interval it can know about, and appends from
-- there.
--
-- ------------------------------------------------------------------------
-- WHAT IS HONESTLY NULL.
--
-- A session that started before the ledger's earliest row for its provider
-- gets NULL, not a guess. The ledger cannot know about a version it never
-- saw, and attributing such a session to the oldest version it happens to
-- hold would be a fabrication that reads like a measurement. Same for
-- session_kind: gemini and antigravity transcripts carry no marker this
-- session could find, so their rows stay NULL rather than being assumed
-- human-facing.
--
-- ------------------------------------------------------------------------
-- ROLLBACK (statement, not a file — this repo has no rollback files, and the
-- migration is purely ADDITIVE: one new table plus one nullable column with
-- no backfill and no default, so dropping both restores the prior schema
-- exactly and loses only data this feature created):
--
--   DROP INDEX IF EXISTS idx_instruction_versions_lookup;
--   DROP INDEX IF EXISTS idx_instruction_versions_path;
--   DROP TABLE IF EXISTS instruction_versions;
--   DROP INDEX IF EXISTS idx_agent_sessions_kind;
--   ALTER TABLE agent_sessions DROP COLUMN session_kind;
--
-- (SQLite 3.45.1 here; DROP COLUMN needs >= 3.35. The column carries no
-- index-only reference and no constraint, so the drop is unconditional.)

-- An append-only ledger of instruction-file CONTENT CHANGES. One row per
-- (path, moment the content became live). A row is in force from its
-- `first_seen` until the next row for the same path; the newest row per path
-- is what is live now.
--
-- A digest can recur — edit a profile field and revert it and the same bytes
-- come back — so uniqueness is (path, first_seen), never (path, sha256).
-- That second reading would collapse a revert into the original interval and
-- silently attribute every session in between to the wrong version.
CREATE TABLE IF NOT EXISTS instruction_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Absolute path of the okuro-managed file, as _deployment records it —
    -- or, for content okuro owns only PART of, that path with a fragment:
    -- `~/.codex/config.toml#developer_instructions`. The manifest cannot hold
    -- those (it digests whole files, deliberately: a whole-file digest of a
    -- file the user co-owns reports "modified" the moment they edit their own
    -- half). The ledger can, because a row is (path, digest) and nothing
    -- requires the digest to cover every byte of the file. Without this,
    -- codex's and antigravity's SYSTEM-TIER channels — the highest-weight
    -- instructions either provider receives — would change with no ledger row
    -- and no cohort boundary, which is the exact mixing this table exists to
    -- end.
    path        TEXT NOT NULL,
    -- ADAPTER name (claude | codex | antigravity | cursor), not the trace
    -- store's provider name (claude-code | codex | antigravity | gemini).
    -- The two vocabularies differ and the mapping lives in one place,
    -- _instruction_ledger.PROVIDER_TO_ADAPTER, so neither side has to know
    -- about the other's spelling.
    provider    TEXT NOT NULL,
    -- NULL only on a tombstone (source='unmanaged'): the row that ends a
    -- path's interval when okuro stops managing it. Without one, the last
    -- real row stays in force forever and folds a file nobody receives into
    -- every future bundle. Nothing writes a tombstone today — no code path
    -- retires a managed path — so this is an expressible shape, not dead
    -- code; the alternative is a second migration the day one appears.
    sha256      TEXT,
    bytes       INTEGER,
    -- ISO-8601 UTC, same layout as agent_sessions.first_ts, so the interval
    -- comparison is a plain string compare with no conversion on either side.
    first_seen  TEXT NOT NULL,
    -- 'write' — observed live by write_managed at the moment of the change.
    -- 'seed'  — recovered from the manifest's written_at when the ledger was
    --           first created. Kept apart because a seeded row's first_seen
    --           is as old as the manifest and no older, and a reader that
    --           cannot tell them apart will over-trust the earliest boundary.
    source      TEXT NOT NULL DEFAULT 'write'
                CHECK (source IN ('write', 'seed', 'unmanaged')),
    recorded_at TEXT DEFAULT (datetime('now')),
    -- A digest is required except on a tombstone.
    CHECK (sha256 IS NOT NULL OR source = 'unmanaged')
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_instruction_versions_path
    ON instruction_versions(path, first_seen);

-- The interval lookup: newest row for a path at or before a timestamp.
CREATE INDEX IF NOT EXISTS idx_instruction_versions_lookup
    ON instruction_versions(provider, path, first_seen DESC);

-- What started this session. NULL is a real value here and means "the
-- transcript carried no marker", which is the true state for gemini and
-- antigravity today.
--
-- VOCABULARY IS THE INCUMBENT'S, NOT A NEW ONE. okuro already has exactly one
-- definition of what a subagent session is:
-- `okuro.sense.interaction.turns.session_kind()`, consumed by detect.py,
-- turns.py and distill/triage.py — whose docstring says it delegates rather
-- than re-deriving "so the store has ONE definition of what a subagent
-- session is". This column carries that same answer across providers; it does
-- not invent a second one. Measured 2026-09-17 on the newest 400 claude-code
-- transcripts: the incumbent's id-prefix rule and the `isSidechain` flag agree
-- 395 times out of 395 that carry the flag, so there was nothing to gain by
-- disagreeing with it.
--
--   human-facing — a person started it at a terminal or in an IDE
--   subagent     — spawned by another agent or by a non-interactive runner
--                  (claude-code sidechains, `codex exec`, codex subagent
--                  thread spawns)
--   imported     — the file is a transcript of a conversation that did not
--                  run here (Codex Desktop's external import). Not a session
--                  on this machine at all, and its timestamps are the import
--                  time, not the conversation's. This is the one case the
--                  incumbent function cannot express — a session id says
--                  nothing about where the conversation happened.
--
-- No CHECK constraint: a fifth provenance kind is a data question, and a
-- CHECK on a table this size forces the 12-table rebuild dance that
-- migration 160 had to perform for exactly that reason.
ALTER TABLE agent_sessions ADD COLUMN session_kind TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_sessions_kind
    ON agent_sessions(provider, session_kind);
