### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Store maintenance primitives whose failure mode is silence — freelist reclamation, so far.
# index: imports | freelist_count | incremental_vacuum
# AGENT_HEADER_END -->
"""Maintenance operations on the SQLite store.

What lives here is narrower than "database utilities": these are the
operations that **fail silently**. Nothing raises, nothing logs, and the
caller's next line reads as though the work happened. A helper is the only
place to put the knowledge that it did not.

This module exists because two callers had grown their own copy of the same
reclamation logic with two different return contracts — see
:func:`incremental_vacuum`.
"""

from __future__ import annotations


def freelist_count(db) -> int:
    """Pages currently on the freelist, or 0 if the store cannot say."""
    row = db.fetchone("PRAGMA freelist_count")
    if not row:
        return 0
    return int(next(iter(row.values())))


def incremental_vacuum(db, pages: int | None = None) -> int:
    """Return freed pages to the OS. **Returns the number of pages freed.**

    That sentence is the reason this function exists in one place. Two
    implementations of this logic previously coexisted — one returning pages
    freed, the other returning the freelist count *remaining* — under the same
    name and the same ``int`` return type. Nothing would have caught a caller
    reading one as the other; it would simply have reported a wrong number
    forever. Pages freed is the actionable figure, so that is the contract, and
    naming it here is what keeps it from drifting back apart.

    The ``.fetchall()`` IS the reclamation, not a formality. ``PRAGMA
    incremental_vacuum`` frees ONE page per step and reports each through the
    cursor, so a call whose cursor is dropped frees exactly one page while
    reading like "reclaim the freelist". Both halves measured:

    * scratch store — freelist 1002 -> 1001 unconsumed, 1002 -> 0 drained;
    * live store, 2026-08-13 runbook — 150,709 -> 150,708 unconsumed,
      -> 0 drained, 589 MB returned to the OS.

    Requires ``auto_vacuum=INCREMENTAL``. That setting lives in the database
    header and cannot be switched on after the header exists without a full
    VACUUM, so a store created without it silently frees nothing here — the
    live store is already ``auto_vacuum=2``.

    No VACUUM and no checkpoint from this function. In WAL mode VACUUM parks
    its output in the WAL, and measuring the result without verifying the
    checkpoint actually ran has already produced a report of
    "-9,753 MB reclaimed" once.

    Args:
        db: an okuro store (anything with ``.conn`` and ``.fetchone``).
        pages: cap the work at N pages. ``None`` drains the whole freelist.

    Returns:
        Pages freed — ``freelist_count`` before minus after, never negative.
    """
    before = freelist_count(db)
    sql = "PRAGMA incremental_vacuum"
    if pages is not None:
        sql += f"({int(pages)})"
    # Chained .fetchall() rather than split across two statements: that is the
    # shape tests/db/test_pragma_cursor_consumption.py can resolve statically,
    # and a detector that cannot read the call site does not protect it.
    db.conn.execute(sql).fetchall()
    return max(0, before - freelist_count(db))
