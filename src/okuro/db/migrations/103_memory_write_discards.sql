-- Migration 103 — audit trail for write_memory's silent content discards.
--
-- write_memory treats a near-neighbour (cosine > 0.90 or Jaccard >= 0.55) as a
-- restatement: it DISCARDS the new text and raises the existing row's
-- confidence instead. This is correct for a genuine restatement and destructive
-- for a refinement — the corrected detail is dropped and the STALE row is
-- rewarded.
--
-- The behaviour was latent from 2026-05-10 to 2026-07-15 (under the vec0 L2
-- defect, dedup demanded cosine > 0.995 and effectively never fired) and went
-- live with the cosine fix. Measured current rate: 6.5% of writes (n=400,
-- 2026-07-19). Proven false merge from that sample:
--
--   cos=0.937
--   NEW (discarded):   "BookExplorer MUST match the existing
--                       books.inthemachine.io visual identity, NOT the okuro
--                       'Architecture Noir' ..."
--   KEPT (reinforced): "explorer data wiring ... /api/explorer/catalog
--                       returns all 33 ..."
--
-- A distinct design constraint absorbed into an unrelated data-wiring note,
-- whose confidence then went UP as a reward.
--
-- Until now the only record was a Python logger line. MCP servers run as
-- stdio subprocesses whose stderr reaches no journal, so nothing was
-- collectable: `journalctl --user --since 2026-07-10 | grep -c "DISCARDED"`
-- returns 0. The historical losses are unrecoverable. This table makes every
-- future one recoverable — it stores the DISCARDED TEXT, which is the whole
-- point and the one thing memory_dedup_log cannot hold (it has no text column
-- and its dupe_id is NOT NULL, but a write-time discard never becomes a row).
--
-- Deliberately a separate table from memory_dedup_log: that logs consolidation
-- of two EXISTING rows; this logs content that was never stored at all. Same
-- word, different event.
--
-- Idempotent: IF NOT EXISTS. No backfill is possible — the discarded text was
-- never persisted anywhere.

CREATE TABLE IF NOT EXISTS memory_write_discards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discarded_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- The text the caller tried to write and which was NOT stored.
    discarded_text  TEXT NOT NULL,
    topic           TEXT,
    project         TEXT,
    role            TEXT,
    source_agent    TEXT,
    -- The row that absorbed the write and had its confidence raised.
    canonical_id    TEXT NOT NULL,
    canonical_text  TEXT,
    -- 'cosine' or 'lexical' — which arm of _find_write_dupe fired.
    reason          TEXT NOT NULL,
    score           REAL NOT NULL,
    old_confidence  REAL,
    new_confidence  REAL,
    -- Set when a human or agent restores the discarded text as its own memory.
    recovered_at    TEXT,
    recovered_as    TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_write_discards_at
    ON memory_write_discards (discarded_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_write_discards_canonical
    ON memory_write_discards (canonical_id);

-- Unrecovered discards, newest first — the review queue.
CREATE INDEX IF NOT EXISTS idx_memory_write_discards_unrecovered
    ON memory_write_discards (discarded_at DESC) WHERE recovered_at IS NULL;
