-- <!-- AGENT_HEADER
-- role: code
-- purpose: 052_review_queue — daemon-side publish/subscribe queue for
--   session-loop subagent retry. Backs okuro.sense.review_queue.
--   PR 1 of 4: storage only. PR 2 exposes await_review as an MCP tool,
--   PR 3 wires the engine to publish_review on critic+scorer completion,
--   PR 4 flips the subagent prompt default.
-- index: content
-- AGENT_HEADER_END -->
--
-- Why a new table, not piggyback on task_events / role_handovers:
--   task_events is a per-task ordered decision log (append-only, ordered
--   by seq). role_handovers is one-row-per-subtask-attempt with brief
--   JSON. The review-queue is a transient publish/subscribe primitive —
--   one row per (subtask_id, artifact_id) review verdict, TTL'd after
--   an hour, and broadcast-consumed by potentially many awaiters. Wrong
--   shape for both existing tables.
--
-- Why no FK on subtask_id / artifact_id:
--   Orchestrator state is JSON-on-disk under ~/.okuro/orchestrator/tasks/
--   (no SQL tasks table); artifact_id may refer to either an artifacts
--   row id or a synthetic engine-side handle. Treat both as opaque TEXT.
--   Same rationale as 035 / 045.
--
-- TTL:
--   `expires_at` is set to created_at + 1 hour at INSERT. purge_expired()
--   in the python layer deletes rows past their expiry. No on-write
--   trigger — purges are batched by a follow-up cron (wired in PR 2+).
--
-- Verdict values:
--   PASS | FAIL | CAP | NEEDS_USER — matches the M3 critic+scorer verdict
--   shape carried by task_events (event_type='verdict', body.verdict).
--   Plus 'still_reviewing' / 'timeout' status returned by the async
--   await_review wrapper (NOT a verdict — those are response shapes the
--   awaiter sees when the keep-alive window or timeout fires).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS review_queue (
    subtask_id            TEXT NOT NULL,
    artifact_id           TEXT NOT NULL,
    verdict               TEXT NOT NULL
                          CHECK (verdict IN ('PASS', 'FAIL', 'CAP', 'NEEDS_USER')),
    findings_json         TEXT NOT NULL DEFAULT '[]',
    implicated_acs_json   TEXT NOT NULL DEFAULT '[]',
    attempt               INTEGER NOT NULL DEFAULT 1,
    max_attempts          INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at            TEXT NOT NULL DEFAULT (datetime('now', '+1 hour')),
    PRIMARY KEY (subtask_id, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_review_queue_key
    ON review_queue(subtask_id, artifact_id);
CREATE INDEX IF NOT EXISTS idx_review_queue_expires
    ON review_queue(expires_at);

CREATE TRIGGER IF NOT EXISTS review_queue_reject_empty_subtask_ins
BEFORE INSERT ON review_queue
FOR EACH ROW
WHEN NEW.subtask_id IS NULL OR TRIM(NEW.subtask_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'review_queue.subtask_id must be a non-empty string');
END;

CREATE TRIGGER IF NOT EXISTS review_queue_reject_empty_artifact_ins
BEFORE INSERT ON review_queue
FOR EACH ROW
WHEN NEW.artifact_id IS NULL OR TRIM(NEW.artifact_id) = ''
BEGIN
    SELECT RAISE(ABORT, 'review_queue.artifact_id must be a non-empty string');
END;

PRAGMA foreign_keys = ON;
