-- <!-- AGENT_HEADER
-- role: code
-- purpose: 139_blob_backfill_position — resumable position for the base64 backfill, plus the threshold it was taken at.
-- index: content
-- AGENT_HEADER_END -->
--
-- The backfill sweeps ~850k agent_events rows and rewrites the ones carrying
-- base64 image bodies. It will be interrupted — by a laptop lid, by a full
-- disk, by somebody deciding an hour in that the batch size is wrong — so it
-- has to resume, and resuming correctly is the whole content of this table.
--
-- WHY A POSITION AND NOT A FLAG
-- -----------------------------
-- Migration 135 already paid for this lesson on the harvest watermark: a
-- boolean "this has been processed" is wrong for an append-only log, and wrong
-- in the direction that costs data. agent_events grows and is rewritten under
-- a running sweep — trace-ingest appends events to sessions ingested weeks ago.
-- A flag says "done" about a table that is no longer the table that was
-- scanned. `last_rowid` says how far the sweep got, and everything above it is
-- unscanned BY DEFINITION rather than by bookkeeping.
--
-- Default -1, not 0, for the same reason 135 gives: rowid is 1-based in SQLite
-- but a default of 0 would still be a legal position, and "nothing scanned"
-- must not be spellable as a real one.
--
-- WHY THE THRESHOLD IS STORED WITH THE POSITION
-- ---------------------------------------------
-- This is the trap the position alone does NOT close, and it is specific to
-- this sweep. A row below `last_rowid` was examined under a particular
-- `blobs.min_bytes`, and "examined and left inline" is only a durable answer
-- for THAT threshold. Lower min_bytes from 32 KiB to 8 KiB and every row
-- already swept holds payloads that now qualify — but the watermark says they
-- are behind us, so they are never revisited and the operator sees a completed
-- run that quietly skipped most of its work.
--
-- So the threshold is recorded alongside the position, and the script refuses
-- to resume across a change to it unless the run is explicitly restarted. The
-- refusal is the feature: the alternative is a silent partial sweep that
-- reports success.
--
-- REFUSALS ARE RE-SELECTABLE
-- --------------------------
-- A row whose blob could not be written (ENOSPC, permissions) must be retried,
-- not stepped over. The script therefore advances `last_rowid` only across
-- rows it actually finished, and halts at the first refusal — so the position
-- is always "everything at or below this is done", never "this is where I gave
-- up". `refusals` records that it happened at all, because a run that halts
-- early and a run that completes otherwise look identical from the outside.

CREATE TABLE IF NOT EXISTS blob_backfill_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),

    -- How far the sweep got. Everything with rowid <= this has been examined
    -- AND finished at min_bytes_at_scan. -1 = nothing scanned.
    last_rowid          INTEGER NOT NULL DEFAULT -1,

    -- The blobs.min_bytes in force when that position was reached. Resuming
    -- with a different value is refused; see above.
    min_bytes_at_scan   INTEGER,

    -- Cumulative across resumed runs, which is why they live here rather than
    -- being recomputed per invocation.
    rows_rewritten      INTEGER NOT NULL DEFAULT 0,
    blocks_moved        INTEGER NOT NULL DEFAULT 0,
    bytes_reclaimed     INTEGER NOT NULL DEFAULT 0,
    blobs_written       INTEGER NOT NULL DEFAULT 0,
    blobs_deduped       INTEGER NOT NULL DEFAULT 0,
    refusals            INTEGER NOT NULL DEFAULT 0,

    started_at          TEXT,
    updated_at          TEXT DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO blob_backfill_state (id) VALUES (1);
