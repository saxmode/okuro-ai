### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Connection-scoped sentinels — the declarations that unlock agent_events' schema guards.
# index: imports | ConnectionSentinel | DISTILL_GATE | TRACE_UPSERT | register_all | EVENT_CONTENT_JSON_INDEX | _externalize_blobs | armed_executemany
# AGENT_HEADER_END -->
"""The two declarations that unlock ``agent_events``.

Migration 136 puts two triggers on ``agent_events``, and each asks the same
question in SQL: *did the caller say it meant this?*

``agent_events_retention_guard``  BEFORE DELETE — unlocked by :data:`DISTILL_GATE`
``agent_events_replace_guard``    BEFORE INSERT onto an existing uuid —
                                  unlocked by :data:`TRACE_UPSERT`

Both exist because the destructive statement is indistinguishable from the
legitimate one at the point SQLite asks. A DELETE issued by the retention gate
looks exactly like a stray DELETE; an ``ON CONFLICT(uuid) DO UPDATE`` looks
exactly like an ``INSERT OR REPLACE`` — measured, a BEFORE INSERT guard keyed
only on "this uuid exists" blocks both. So the difference is never detected,
it is DECLARED.

WHY A THREAD-LOCAL FLAG AND NOT create_function PER CALL
---------------------------------------------------------
Both sentinels originally armed by re-registering their SQL function. That
breaks: ``sqlite3`` refuses ``create_function`` while an unconsumed cursor is
live on the connection, raising ``OperationalError: Error creating function``,
and okuro's row factory leaves statements active routinely. The upsert
sentinel hit this for real the first time it was armed inside a write
transaction; the gate sentinel had the same latent trap and only escaped it by
happening to arm outside one.

So each function is registered ONCE per connection, in
``db/sqlite.py::_make_conn``, and reads a thread-local flag. Arming mutates
Python state, never SQLite's. okuro's connections are thread-local too, so
"armed" scopes to exactly one connection.

WHAT THIS DEFENDS, AND WHAT IT DOES NOT
----------------------------------------
Not a security boundary against okuro's own Python: any code can arm a flag.
It defends (1) ACCIDENTAL destruction from code that never meant to destroy
anything, which is the failure the standing constraint exists to prevent, and
(2) non-Python writers, absolutely — a sqlite3 shell cannot define a function,
so a statement naming one does not compile there at all.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager


class ConnectionSentinel:
    """A named SQL function returning 1 only while this thread has armed it.

    The trigger side compares with ``IS NOT 1``, never ``!= 1``: under ``!= 1``
    a NULL return evaluates NULL, the WHEN clause is not satisfied, and the
    statement proceeds — fail-open on precisely the input that means the
    arming path is broken.
    """

    def __init__(self, function_name: str):
        self.function_name = function_name
        self._state = threading.local()

    def value(self) -> int:
        """1 when armed on this thread, else 0. An int, because SQL reads it."""
        return 1 if getattr(self._state, "armed", False) else 0

    @contextmanager
    def armed(self):
        """Arm for the duration of the block, restoring the previous state.

        Re-entrant, and restored in a ``finally`` so an exception mid-write
        cannot leave the declaration standing for whatever runs next on this
        thread.
        """
        previous = getattr(self._state, "armed", False)
        self._state.armed = True
        try:
            yield
        finally:
            self._state.armed = previous


# Names must match the triggers in migration 136. If they drift, both guards
# fail closed with "no such function" — the safe direction.
DISTILL_GATE = ConnectionSentinel("okuro_distill_gate_armed")
TRACE_UPSERT = ConnectionSentinel("okuro_trace_upsert_armed")

_ALL = (DISTILL_GATE, TRACE_UPSERT)


def register_all(conn: sqlite3.Connection) -> None:
    """Register every sentinel on a fresh connection, all disarmed."""
    for sentinel in _ALL:
        conn.create_function(sentinel.function_name, 0, sentinel.value)


# Position of content_json in the agent_events column list. All four ingesters
# write the same 13 columns in the same order — three bind by name and one
# (antigravity) binds positionally — so a tuple row is indexable here. Pinned
# by a test, because this constant is the one thing that turns a column-order
# change in one ingester into a silent miss in the externalizer.
EVENT_CONTENT_JSON_INDEX = 9
EVENT_TEXT_INDEX = 8


def _externalize_blobs(rows):
    """Move oversized base64 bodies out of the rows about to be written.

    THIS IS THE CLASS FIX, AND ITS PLACEMENT IS THE POINT. The defect is not
    "claude-code stores images in the database", it is "every ingester writes
    content_json verbatim, so whatever the provider inlined becomes a row". A
    fix in the four ingesters is four fixes that can drift, and a fifth
    provider ships the defect again on day one. This function sits on the one
    statement all four already share.

    Blobs are written BEFORE the row that references them, and deliberately so.
    The two failure orderings are not symmetric: a blob with no row is an
    orphan costing disk, while a row with no blob is a dangling ref — data
    loss. So if this transaction rolls back after the files land, what is left
    behind is the harmless one.

    Returns aggregate stats; callers currently ignore them, and the backfill
    reports its own.
    """
    from okuro.db.blobs import externalize_row, min_bytes

    limit = min_bytes()
    stats = {"rows": 0, "blocks": 0, "blob_bytes": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue  # positional rows go through _externalize_tuple_rows
        result = externalize_row(row, threshold=limit)
        if result:
            stats["rows"] += 1
            stats["blocks"] += result["blocks"]
            stats["blob_bytes"] += result["blob_bytes"]
    return stats


def _externalize_tuple_rows(rows):
    """Same, for positionally-bound rows. Returns a new list when anything moved.

    Separate from :func:`_externalize_blobs` because a tuple cannot be mutated
    in place, so this path has to rebuild the sequence — and rebuilding a
    million-row batch that needed no change is exactly the cost the length
    pre-check exists to avoid. Only antigravity binds positionally, and it
    writes ``content_json = NULL``, so in practice this returns the input
    untouched. It exists so that stays true by test rather than by luck.
    """
    from okuro.db.blobs import externalize_row, min_bytes

    limit = min_bytes()
    out = None
    for i, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            continue
        if len(row) <= EVENT_CONTENT_JSON_INDEX:
            continue
        blob = row[EVENT_CONTENT_JSON_INDEX]
        if not isinstance(blob, str) or len(blob) < limit:
            continue
        shim = {"content_json": blob, "text": row[EVENT_TEXT_INDEX]}
        if externalize_row(shim, threshold=limit) is None:
            continue
        if out is None:
            out = list(rows)
        mutated = list(row)
        mutated[EVENT_CONTENT_JSON_INDEX] = shim["content_json"]
        mutated[EVENT_TEXT_INDEX] = shim["text"]
        out[i] = tuple(mutated)
    return out


def armed_executemany(conn: sqlite3.Connection, sql: str, rows) -> None:
    """``executemany`` for the trace event upsert, armed for that statement.

    A function rather than a ``with`` block at each call site: the four
    ingesters each pass a ~20-line SQL literal inline, and wrapping those would
    have re-indented all four for no behavioural gain. The arming window stays
    exactly one statement wide, which is the property that matters.

    Sessions genuinely do get rewritten — Claude Code resumes a transcript and
    ``trace-ingest`` re-reads the whole file, re-offering stored events with
    the same uuid. This is what keeps that path open while REPLACE stays shut.

    It is also where base64 image and document bodies leave the row, for the
    reason given in :func:`_externalize_blobs`. Re-ingest stays idempotent
    because externalization is a pure function of the payload: the same
    transcript produces the same refs, the ON CONFLICT DO UPDATE writes the
    same bytes it already holds, and the blob write finds the file present and
    does nothing.
    """
    rows = list(rows)
    _externalize_blobs(rows)
    rebuilt = _externalize_tuple_rows(rows)
    if rebuilt is not None:
        rows = rebuilt
    with TRACE_UPSERT.armed():
        conn.executemany(sql, rows)
