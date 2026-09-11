### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The only route by which agent_events rows may be deleted — every precondition fails closed and says why.
# index: imports | GATE_FUNCTION_NAME | Reason | GateResult | gate_armed | read_config | delete_session_events | run_gate
# AGENT_HEADER_END -->
"""The retention gate.

Migration 136 puts a ``BEFORE DELETE`` trigger on ``agent_events`` that aborts
unless ``okuro_distill_gate_armed()`` returns 1. The function is registered
disarmed on every okuro connection (``db/sqlite.py::_make_conn``) and this
module is the only place that arms it, which is what makes it the only route
in. Everything else — a stray DELETE, a cascade from ``agent_sessions``, a
future migration, the sqlite3 CLI — meets the trigger.

**Today, every real invocation refuses.** ``distill_config.deletion_enabled``
ships 0 and nothing in okuro writes it; flipping it is the owner's Phase-4
ratification, per the standing constraint that ``agent_events`` must not be
deleted until the qualification loop is trusted. A refusal here is the system
working, not a fault to route around.

Preconditions, in the order they are checked, each with its own counter:

======================================  ====================================
Reason                                  Meaning
======================================  ====================================
``deletion_disabled``                   the Phase-4 switch is off (always,
                                        today)
``guard_missing``                       a schema guard has been dropped, so
                                        the protection this gate assumes is
                                        not actually installed
``not_distilled``                       no ledger row — the constraint's
                                        actual precondition
``rubric_below_floor``                  distilled, but by a rubric no longer
                                        trusted
``no_events``                           nothing to delete
``events_changed_during_archive``       the session grew between archiving
                                        and deleting
``archive_failed``                      could not write or re-read the
                                        archive
``archive_roundtrip_mismatch``          the archive does not contain exactly
                                        the events about to be deleted
``fts_integrity_failed``                the FTS index did not survive the
                                        delete
======================================  ====================================

Counters are keyed by reason for the same reason the compactor's are: a run
that refused 4000 sessions and a run that found nothing eligible both report
zero deletions, and if the output cannot tell them apart then fail-closed is
indistinguishable from success.

Why the round trip is a decompress-and-compare rather than a checksum: a
digest of our own output proves the bytes reached the disk. It cannot prove
the serializer emitted every row. A serializer that silently drops events
produces a perfectly valid archive with a perfectly matching digest, and the
gate would then delete the rows it failed to save. So the archive is reopened,
decompressed, and its event-ID set compared against the exact set about to be
deleted.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable

from okuro.db.sentinels import DISTILL_GATE

from .archive import (
    EVENT_COLUMNS,
    ArchiveError,
    read_archive,
    write_archive,
)

log = logging.getLogger(__name__)

# Re-exported for tests and callers that need the SQL-side name. The name
# itself lives with the sentinel, so it cannot drift from the registration.
GATE_FUNCTION_NAME = DISTILL_GATE.function_name


class Reason:
    """Refusal reasons. Strings, because they are counter keys and log text."""

    DELETION_DISABLED = "deletion_disabled"
    GUARD_MISSING = "guard_missing"
    NOT_DISTILLED = "not_distilled"
    RUBRIC_BELOW_FLOOR = "rubric_below_floor"
    NO_EVENTS = "no_events"
    EVENTS_CHANGED = "events_changed_during_archive"
    ARCHIVE_FAILED = "archive_failed"
    ARCHIVE_ROUNDTRIP_MISMATCH = "archive_roundtrip_mismatch"
    FTS_INTEGRITY_FAILED = "fts_integrity_failed"


@dataclass
class GateResult:
    session_id: str
    deleted: bool
    reason: str | None = None
    detail: str | None = None
    events_deleted: int = 0
    archive_path: str | None = None
    archive_sha256: str | None = None


@dataclass
class GateRun:
    """Aggregate of a batch pass. ``refused_by_reason`` is the point."""

    considered: int = 0
    deleted_sessions: int = 0
    events_deleted: int = 0
    refused_by_reason: dict[str, int] = field(default_factory=dict)
    results: list[GateResult] = field(default_factory=list)
    pages_freed: int = 0

    def record(self, result: GateResult) -> None:
        self.considered += 1
        self.results.append(result)
        if result.deleted:
            self.deleted_sessions += 1
            self.events_deleted += result.events_deleted
        elif result.reason:
            self.refused_by_reason[result.reason] = (
                self.refused_by_reason.get(result.reason, 0) + 1
            )


@contextmanager
def gate_armed():
    """Declare that the deletes in this block are the gate's own.

    Scoped to the calling thread, and okuro's connections are thread-local, so
    "armed" means exactly one connection. Delegates to the shared sentinel in
    :mod:`okuro.db.sentinels` — one mechanism for both of agent_events'
    guards, rather than two that drift.

    Takes no connection argument. It used to, and armed by re-registering the
    SQL function on that connection; that is unsafe, because sqlite3 refuses
    ``create_function`` while an unconsumed cursor is live and okuro's row
    factory leaves statements active routinely. This path escaped the bug only
    by happening to arm outside any transaction — the upsert sentinel, armed
    mid-write, hit it for real.

    WHAT THIS DOES AND DOES NOT DEFEND. Not a security boundary against
    okuro's own Python — any code can arm the flag, and nothing here pretends
    otherwise. It defends (1) ACCIDENTAL deletion from okuro code that never
    meant to delete, which is the failure the standing constraint exists to
    prevent, and (2) NON-PYTHON writers, absolutely — a sqlite3 shell cannot
    define the function, so for a shell the guard is not a policy but a wall.

    The ``finally`` inside the sentinel matters: an exception mid-delete must
    not leave the declaration standing for whatever runs next on this thread.
    """
    with DISTILL_GATE.armed():
        yield


def read_config(db) -> dict[str, int]:
    """Return the deletion switch and the rubric floor.

    Fails closed if the row is missing: a store whose config did not migrate
    is a store where nothing may be deleted.
    """
    row = db.fetchone(
        "SELECT deletion_enabled, min_deletable_rubric_version "
        "FROM distill_config WHERE id = 1"
    )
    if row is None:
        return {"deletion_enabled": 0, "min_deletable_rubric_version": 1 << 30}
    return {
        "deletion_enabled": int(row["deletion_enabled"] or 0),
        "min_deletable_rubric_version": int(row["min_deletable_rubric_version"] or 0),
    }


def _fetch_events(db, session_id: str) -> list[dict[str, Any]]:
    cols = ", ".join(EVENT_COLUMNS)
    return db.fetchall(
        f"SELECT {cols} FROM agent_events WHERE session_id = ? ORDER BY ord",
        (session_id,),
    )


def _event_uuids(db, session_id: str) -> set[str]:
    return {
        r["uuid"]
        for r in db.fetchall(
            "SELECT uuid FROM agent_events WHERE session_id = ?", (session_id,)
        )
    }


def delete_session_events(session_id: str, db=None) -> GateResult:
    """Archive, verify, then delete one session's events. Fails closed.

    Returns a :class:`GateResult` rather than raising on a refusal — a refusal
    is an expected outcome (it is the ONLY outcome today), and the caller needs
    the reason to count it.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    cfg = read_config(db)

    # (a) The ratification switch. First, and unconditional: no amount of
    # correct distillation licenses a delete while this is 0.
    if not cfg["deletion_enabled"]:
        return GateResult(
            session_id,
            False,
            Reason.DELETION_DISABLED,
            "distill_config.deletion_enabled is 0 (Phase-4 ratification pending)",
        )

    # (a2) The guards must actually be installed. DROP TRIGGER is silent and
    # unrefusable in SQL, so "the schema protects this" is a claim with a
    # runtime lifetime, not a permanent fact.
    missing = _guards_present(db)
    if missing:
        return GateResult(
            session_id,
            False,
            Reason.GUARD_MISSING,
            f"schema guards absent from agent_events: {', '.join(missing)} — "
            "refusing to delete without the protection this gate assumes",
        )

    # (b) The standing constraint's actual precondition: a retained
    # qualification, produced by a rubric still considered trustworthy.
    dist = db.fetchone(
        "SELECT rubric_version FROM session_distillations WHERE session_id = ?",
        (session_id,),
    )
    if dist is None:
        return GateResult(
            session_id, False, Reason.NOT_DISTILLED, "no session_distillations row"
        )
    rubric = int(dist["rubric_version"])
    floor = cfg["min_deletable_rubric_version"]
    if rubric < floor:
        return GateResult(
            session_id,
            False,
            Reason.RUBRIC_BELOW_FLOOR,
            f"rubric_version {rubric} < min_deletable_rubric_version {floor}",
        )

    rows = _fetch_events(db, session_id)
    if not rows:
        return GateResult(session_id, False, Reason.NO_EVENTS, "no events to delete")

    archived_uuids = {r["uuid"] for r in rows}

    # (c) Archive, then READ IT BACK. Both steps happen before the write
    # transaction opens: compressing and fsyncing a few MB while holding the
    # writer lock would stall every other writer for no benefit.
    try:
        result = write_archive(session_id, rows)
        restored = read_archive(result.path)
    except ArchiveError as exc:
        return GateResult(session_id, False, Reason.ARCHIVE_FAILED, str(exc))

    restored_uuids = {r.get("uuid") for r in restored}
    if len(restored) != len(rows) or restored_uuids != archived_uuids:
        # This is the case a digest cannot see: the file is intact, it just
        # does not contain what we are about to destroy.
        return GateResult(
            session_id,
            False,
            Reason.ARCHIVE_ROUNDTRIP_MISMATCH,
            f"archive holds {len(restored)} events / {len(restored_uuids)} ids, "
            f"delete set is {len(rows)} / {len(archived_uuids)}; "
            f"missing={sorted(archived_uuids - restored_uuids)[:5]}",
        )

    # (d) Delete, under the sentinel, in one transaction with the tombstone.
    try:
        with gate_armed():
            with db.write():
                # Re-check the event set INSIDE the write lock. Sessions
                # resume: claude-code appends to a transcript days later and
                # trace-ingest writes those rows with their ORIGINAL
                # timestamps (the defect migration 135 was written for). If
                # that happened between the archive and here, the delete would
                # remove events the archive does not contain.
                current = _event_uuids(db, session_id)
                if current != archived_uuids:
                    raise _Refused(
                        Reason.EVENTS_CHANGED,
                        f"event set changed after archiving: "
                        f"+{len(current - archived_uuids)} "
                        f"-{len(archived_uuids - current)}",
                    )

                cur = db.execute(
                    "DELETE FROM agent_events WHERE session_id = ?", (session_id,)
                )
                deleted = cur.rowcount

                # (e) The FTS side. agent_events_fts is external-content and
                # trigger-maintained; the _ad trigger from 016 fires on each
                # row above.
                #
                # `rank = 1`, and that argument is the whole check. FTS5's
                # bare 'integrity-check' verifies the index against ITSELF;
                # for an external-content table only the rank=1 form also
                # compares it against the content table. Measured on a store
                # damaged by a REPLACE that skipped the delete triggers:
                #
                #   integrity-check          -> PASS
                #   integrity-check, rank=0  -> PASS
                #   integrity-check, rank=1  -> database disk image is malformed
                #
                # and rank=1 passes on a healthy store, so it is a real
                # discriminator rather than a statement that always throws.
                # The bare form shipped here first and would have verified
                # nothing: an index that checks clean and cannot be read is
                # the worst of the available states.
                try:
                    db.execute(
                        "INSERT INTO agent_events_fts(agent_events_fts, rank) "
                        "VALUES('integrity-check', 1)"
                    )
                except sqlite3.DatabaseError as exc:
                    raise _Refused(Reason.FTS_INTEGRITY_FAILED, str(exc)) from exc

                # (f) Tombstone. PRIMARY KEY on session_id makes "exactly
                # once" a schema fact; a second pass raises rather than
                # recording a second archive path for the same loss.
                db.execute(
                    "INSERT INTO distill_tombstones "
                    "(session_id, events_deleted, archive_path, archive_sha256) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, deleted, str(result.path), result.sha256),
                )
    except _Refused as refusal:
        return GateResult(session_id, False, refusal.reason, refusal.detail)

    return GateResult(
        session_id,
        True,
        None,
        None,
        events_deleted=deleted,
        archive_path=str(result.path),
        archive_sha256=result.sha256,
    )


class _Refused(Exception):
    """Internal: abort the write transaction and surface a counted reason."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def run_gate(session_ids: Iterable[str], db=None) -> GateRun:
    """Run the gate over several sessions and report by reason.

    Nothing schedules this. There is no daemon task, by design: wiring
    deletion into a timer is Phase 4, after ratification.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    run = GateRun()
    for sid in session_ids:
        run.record(delete_session_events(sid, db=db))

    if run.events_deleted:
        from okuro.db.maintenance import incremental_vacuum

        run.pages_freed = incremental_vacuum(db)

    if run.refused_by_reason:
        log.info(
            "distill gate: %d deleted, refused %s",
            run.deleted_sessions,
            ", ".join(f"{k}={v}" for k, v in sorted(run.refused_by_reason.items())),
        )
    return run


def _guards_present(db) -> list[str]:
    """Return the names of migration 136's guards missing from the schema.

    DROP TRIGGER fires nothing and cannot be refused in SQL, so a connection
    that drops a guard can then delete freely. The schema cannot defend that;
    the gate can notice it. Checking here means the gate never performs a
    delete under the belief it is protected when it is not.
    """
    required = {"agent_events_retention_guard", "agent_events_replace_guard"}
    present = {
        r["name"]
        for r in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'agent_events'"
        )
    }
    return sorted(required - present)
