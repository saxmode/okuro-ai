-- <!-- AGENT_HEADER
-- role: code
-- purpose: 144_distill_validation_roster — a calibration batch gets a fixed roster, and a label may only name a session on it.
-- index: content
-- AGENT_HEADER_END -->
--
-- The class of defect this closes: A SAMPLE THAT CAN BE EDITED AFTER THE FACT
-- IS NOT A SAMPLE.
--
-- Migration 137 gates tier-2 on a batch holding at least
-- distill_config.min_validation_labels DISTINCT labelled sessions. It says
-- nothing about WHICH sessions, because until now nothing chose them: any
-- session_id at all could be labelled into any batch. That makes the floor a
-- count of rows rather than coverage of a population — fifty labels of the
-- fifty easiest sessions clear it exactly as well as a stratified fifty, and
-- the resulting judge is calibrated against a sample nobody designed.
--
-- So the batch now has a ROSTER, drawn once by
-- okuro.sense.distill.calibration.select_calibration_sample: proportional
-- across the live cluster map, at least one per cluster, spanning
-- flagged/routine and main/subagent. The roster is written when the batch is
-- created and a label may only name a session on it.
--
-- WHY A TABLE AND NOT A JSON COLUMN ON THE BATCH. The membership question is
-- asked once per label write, by a trigger, in SQL. A JSON array would make
-- that a string scan; a table makes it an indexed lookup and lets the same
-- rows carry the per-session state the labelling CLI resumes from.
--
-- WHY THE ROSTER IS NOT ITSELF THE LABEL ROW. A pre-created label row would
-- need a NULL verdict to mean "not yet labelled", and 137 declares verdict NOT
-- NULL with a CHECK — deliberately, because a label with no verdict is not a
-- label. Roster membership and the act of labelling are different facts and
-- get different rows: presence here means "you were asked", presence in
-- distill_validation_labels means "you answered".
--
-- That separation is also what makes the CLI resume-safe as a POSITION rather
-- than a flag, which is the lesson migrations 135 and 142 were both written
-- for: "how far did I get" is answered by the roster minus the labels, and it
-- stays correct if the run dies between two sessions.

CREATE TABLE IF NOT EXISTS distill_validation_members (
    batch_id     TEXT NOT NULL
                 REFERENCES distill_validation_batches(batch_id) ON DELETE CASCADE,
    session_id   TEXT NOT NULL,

    -- Why this session is on the roster. Recorded so the sample is auditable
    -- after the fact — a reader can check the strata were actually spanned
    -- rather than take the selector's word for it.
    cluster_id   INTEGER,
    stratum      TEXT,          -- e.g. 'main/flagged', 'subagent/routine'

    -- Presentation order, fixed at draw time. The CLI walks it, so two
    -- resumed runs show the same sessions in the same sequence and a
    -- half-finished batch is a prefix rather than an arbitrary subset.
    position     INTEGER NOT NULL,

    added_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (batch_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_distill_validation_members_order
    ON distill_validation_members(batch_id, position);

-- ---------------------------------------------------------------------------
-- A label may only name a session on its batch's roster
-- ---------------------------------------------------------------------------
-- Same reasoning as every other guard in this subsystem: the caller that
-- honours this today is one CLI, and the table is reachable from a daemon
-- task, a future agent and the sqlite3 shell. The check goes where every
-- writer meets it.
--
-- Spelled as a plain NOT EXISTS, which is 0/1 and never NULL — a label naming
-- a batch with no roster at all is refused rather than admitted, which is the
-- fail-closed direction and matters because an empty roster is exactly what a
-- half-created batch looks like.
CREATE TRIGGER IF NOT EXISTS distill_validation_labels_roster_only
BEFORE INSERT ON distill_validation_labels
WHEN NOT EXISTS (
        SELECT 1 FROM distill_validation_members m
        WHERE m.batch_id = NEW.batch_id AND m.session_id = NEW.session_id
     )
BEGIN
    SELECT RAISE(ABORT, 'distill_validation_labels: this session is not on that batch''s roster — a calibration batch is a drawn sample, and labelling something outside it makes the sample whatever the labeller happened to reach for. Draw the batch with okuro.sense.distill.calibration.create_calibration_batch().');
END;

-- The UPDATE half, for the same reason 137 needed one: without it, a label can
-- be inserted against a legitimate roster session and then re-pointed at a
-- session that was never drawn.
CREATE TRIGGER IF NOT EXISTS distill_validation_labels_roster_only_upd
BEFORE UPDATE ON distill_validation_labels
WHEN NOT EXISTS (
        SELECT 1 FROM distill_validation_members m
        WHERE m.batch_id = NEW.batch_id AND m.session_id = NEW.session_id
     )
BEGIN
    SELECT RAISE(ABORT, 'distill_validation_labels: cannot move a label onto a session that is not on that batch''s roster — see the INSERT trigger.');
END;
