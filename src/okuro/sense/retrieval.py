# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: One candidate-pool rule for every vec_search that post-filters.
# index: imports | const _MAX_POOL | def candidate_pool | def scoped_vec_search
# AGENT_HEADER_END -->
"""Retrieval pool sizing — the shared rule for semantic search that filters.

**The defect this exists to make unrepresentable.** ``vec_search`` runs KNN
over a WHOLE vec_* table. Every caller in okuro then narrows the survivors in
Python or in a follow-up ``WHERE`` — by project, role_id, kind, audience,
task_id, active. So the candidate pool is GLOBAL and the filter is LOCAL: if
the k nearest neighbours store-wide happen to contain nothing from the
requested scope, the caller reports absence for a row that is indexed, close,
and would have ranked had the pool been one notch wider.

Measured 2026-08-10 on the live store (~2.5k artifacts):

    artifact_search("START HERE entry point next session",
                    project="okuro-design-systems", limit=3)   -> []
    artifact_search("START HERE okuro-design-systems entry point",
                    project="okuro-design-systems", limit=3)   -> the row, at 0.66

Same store, same artifact, same scope. The first query's 9-row global pool
(``limit * 3``) held no row of that project; the second's did. An agent read
the empty answer as "this was never written" and rewrote a handover document
that already existed.

**Why a shared helper rather than a bigger multiplier at each site.** The
multipliers were picked independently and range over 1x, 2x, 3x, 4x, 6x and
``max(8x, 64)`` for the same operation. Only the last has a floor, and only
that one is correct: with ``limit=2`` a 3x pool is SIX rows, which no filter
can survive. Sizing is one decision about one mechanism, so it lives in one
function; a new semantic-search surface inherits the rule instead of inventing
a seventh multiplier.

**A wider pool is necessary and NOT sufficient — measured, not assumed.** On
the same store a scoped query surfaced 1 of ~21 in-project rows even at a
512-neighbour pool. No multiplier fixes a filter that runs after the scan.

``vec_search(partition=...)`` does: it pushes the equality filter INSIDE the
KNN scan, so the scoped answer is exact rather than "global top-k, then hope".
That needs the column declared as a vec0 partition key at CREATE time —
``vec_cortex`` has had one since F20/F21, where scoped recall had collapsed to
0-1/k because the index is ~90% one project, and ``vec_artifacts``,
``vec_memory`` and ``vec_knowledge`` now do too (embed/repair.py VecSpec).

:func:`scoped_vec_search` ASKS for the push-down and degrades to a wide global
pool when a table cannot serve it, so the two mechanisms compose and a table
gaining a partition key later is a migration, not a change at any call site.
"""

from __future__ import annotations

from typing import Any, Optional

# embed.repair owns vec_* SHAPE, so it owns the partition sentinel too. It
# imports nothing from okuro at module level, so this direction stays acyclic.
from okuro.embed.repair import partition_value

# Ceiling on a single KNN scan. Past this the scan cost stops buying recall:
# a filter selective enough to survive 512 global neighbours is selective
# enough that the answer belongs in a WHERE clause, not in an ANN pool.
_MAX_POOL = 512

# Unscoped: no post-filter, so the pool only absorbs a similarity floor and
# ranking churn. Scoped: a post-filter will discard an unknown fraction, so
# the pool carries headroom and a floor. 8x/64 is not a guess — it is the one
# multiplier in the codebase that never produced a false empty
# (sense/role_handover.py rank_role_handovers_by_relevance).
_UNSCOPED_MULT, _UNSCOPED_FLOOR = 3, 24
_SCOPED_MULT, _SCOPED_FLOOR = 8, 64


def candidate_pool(limit: int, *, scoped: bool = False) -> int:
    """How many neighbours to ask for when ``limit`` rows must survive.

    ``scoped=True`` whenever ANY narrowing happens after the scan — a
    ``WHERE project = ?``, a role_id join, an ``active = 1``, a similarity
    floor that drops a real fraction, a supersede exclusion. When in doubt,
    pass True: an over-wide pool costs one scan, an under-wide one costs a
    wrong answer that reads like a fact.
    """
    if limit < 1:
        limit = 1
    mult, floor = (
        (_SCOPED_MULT, _SCOPED_FLOOR) if scoped
        else (_UNSCOPED_MULT, _UNSCOPED_FLOOR)
    )
    return min(max(limit * mult, floor), _MAX_POOL)


# Above this many rows an exhaustive scan stops being free and the pool rules
# above take over. 5000 x 1024 floats is ~20 MB of distance arithmetic — a few
# milliseconds, and far cheaper than a wrong answer. vec_role_handovers (942)
# and vec_distill_sessions (~10k at full backlog) bracket this figure.
_EXHAUSTIVE_MAX = 5000


def exhaustive_pool(db, table: str, limit: int, *, scoped: bool = True) -> int:
    """Pool that covers the WHOLE table when the table is small enough.

    **The defect this exists for, measured on the live store 2026-08-13.**
    ``vec_role_handovers`` was unpartitioned to reclaim 1062 MiB of chunk
    preallocation. Its reader scopes to one task by post-filtering, so
    correctness was preserved — and recall collapsed:

        limit  pool  pool/table  returned  (of 46 the task owns)
            3    64          7%         1
            8    64          7%         1
           20   160         17%         3
           46   368         39%        13

    At the default ``limit=8`` the caller got ONE handover out of eight
    available. That is the false-empty failure this module's docstring
    describes, arriving through the fix for a different problem: a global pool
    plus a local filter returns the globally-nearest k, and only the fraction
    of them that happen to be in scope survives.

    A partition key solves it by filtering inside the scan. When the table is
    small the cheaper answer is to not narrow at all — ask for every row, let
    the post-filter do the scoping, and the result is exact. sqlite-vec's KNN
    is brute-force anyway, so "all of it" is the same scan with a bigger k.

    Falls back to :func:`candidate_pool` once the table outgrows
    :data:`_EXHAUSTIVE_MAX`, at which point a partition key is the right tool
    again — provided its cardinality is bounded (see
    ``tests/embed/test_vec_partition_cardinality.py``).
    """
    try:
        rows = db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"]
    except Exception:  # noqa: BLE001 — table absent or unreadable; the
        # ordinary rule is always a safe answer.
        return candidate_pool(limit, scoped=scoped)
    if rows and rows <= _EXHAUSTIVE_MAX:
        return max(int(rows), limit)
    return candidate_pool(limit, scoped=scoped)


def scoped_vec_search(
    db: Any,
    table: str,
    embedding: bytes,
    *,
    limit: int,
    partition: Optional[tuple[str, str]] = None,
) -> list[dict]:
    """``vec_search`` that prefers filter push-down and survives its absence.

    Pass ``partition=("project", slug)`` whenever the scope is a plain
    equality on a column. If ``table`` declares that column as a vec0
    partition key the filter runs INSIDE the scan — exact scoped recall, no
    pool guessing. If it does not, vec0 raises and this falls back to the
    global scan the caller would have done anyway, so the caller's Python
    filter still produces a correct (merely narrower) answer.

    Copied in shape from cortex/vectorstore.py, which has run this fallback
    in production since the vec_cortex partition migration. Never returns
    partial results from a half-failed scan: the fallback re-runs the query
    whole.
    """
    if partition is not None:
        try:
            return db.vec_search(table, embedding, limit=limit, partition=partition)
        except Exception:  # noqa: BLE001 — no partition key on this table yet
            pass
    return db.vec_search(table, embedding, limit=limit)


def vec_write(
    db: Any,
    table: str,
    row_id: str,
    embedding: bytes,
    *,
    partition: Optional[tuple[str, Any]] = None,
) -> None:
    """INSERT one vector, carrying its partition value when the table has one.

    A vec0 partition key is a real column: a row written without it lands with
    NULL and becomes unreachable from every partitioned query — the row is
    present, indexed, and invisible, which is a worse failure than not writing
    it at all. Writers therefore declare the scope here and this resolves
    whether the live table can take it.

    Tries the partitioned INSERT first and falls back to the two-column form,
    so the same writer works before and after ``okuro.embed.repair`` rebuilds
    the table. That ordering matters: the fallback direction is safe (a value
    dropped on an unpartitioned table changes nothing), the reverse is not.

    Two things this function got wrong until 2026-08-12, both of which made it
    a SOURCE of the NULL partitions it was meant to prevent:

      * a ``None`` value was written straight through, and a NULL partition key
        costs one preallocated 4 MiB chunk PER ROW (see
        ``embed.repair.NO_PARTITION`` for the measurement). It is coalesced now.
      * the fallback caught bare ``Exception``, so ANY failure — a lock, a dim
        mismatch, a disk error — silently retried WITHOUT the partition column
        and landed a NULL. Only the one error this fallback exists for, an
        unpartitioned table rejecting the column, may reach it now.
    """
    if partition is not None:
        col, val = partition
        try:
            db.execute(
                f"INSERT INTO {table} (id, {col}, embedding) VALUES (?, ?, ?)",
                (row_id, partition_value(val), embedding),
            )
            return
        except Exception as exc:  # noqa: BLE001 — narrowed by message below
            # sqlite reports an undeclared partition column as "table X has no
            # column named Y" / "no such column". Anything else is a real
            # failure and must not be masked by a NULL-partition retry.
            if "no column" not in str(exc) and "no such column" not in str(exc):
                raise
    db.execute(
        f"INSERT INTO {table} (id, embedding) VALUES (?, ?)",
        (row_id, embedding),
    )
