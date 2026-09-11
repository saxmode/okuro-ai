### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Rubric evolution plumbing — the human-only floor setter, and which sessions a rubric change implicates.
# index: imports | current_rubric_version | rubric_versions_in_use | min_deletable_rubric_version | set_min_deletable_rubric_version | sessions_needing_redistillation
# AGENT_HEADER_END -->
"""What happens when "good" changes meaning.

A distillation is only as trustworthy as the rubric it was produced under. When
the rubric changes, two questions follow, and they have deliberately different
answers:

**Which old distillations are still trustworthy?**
``distill_config.min_deletable_rubric_version`` — the floor
:mod:`.gate` consults before permitting a delete. It moves ONLY by an explicit
human call to :func:`set_min_deletable_rubric_version`. Nothing in okuro calls
that function: not the daemon, not the pipeline, not mining. The reason is in
migration 136's own comment — gating on "distilled with the NEWEST rubric"
means the first bump instantly disqualifies the entire corpus and deletion
silently stops forever with nothing in the output to say why. The floor is a
judgement about which older rubrics are still believed, and that judgement is
the owner's.

**Which sessions should be looked at again?**
:func:`sessions_needing_redistillation` answers it and does NOT act. It returns
ids. Re-distilling is a scheduling decision with a token cost attached, and a
selector that ran the work it selected would make that cost invisible at the
call site.

Why there is no ``bump_rubric_version()``
-----------------------------------------
``RUBRIC_VERSION`` is a constant in :mod:`okuro.sense.distill` — the rubric IS
code, so changing what "good" means is a source edit that goes through review,
not a runtime toggle some pass can flip. A helper that incremented it at
runtime would let the definition of quality drift with no diff to read, which
is precisely the property that makes the ledger's ``rubric_version`` column
worth storing.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Callers that are automation wearing a human's parameter. The floor setter
# refuses these outright: the whole point of the switch is that a PERSON
# decided an older rubric is no longer trustworthy, and an approver string of
# "daemon" is the shape that claim takes when nobody decided anything.
_NON_HUMAN_APPROVERS = frozenset({
    "daemon", "cron", "scheduler", "auto", "automatic", "system",
    "okuro", "pipeline", "agent", "", "none", "null", "unknown",
})


def current_rubric_version() -> int:
    """The rubric version this build produces. A code constant, deliberately."""
    from . import RUBRIC_VERSION

    return RUBRIC_VERSION


def min_deletable_rubric_version(db=None) -> int | None:
    """The floor ``gate.py`` enforces, or None when the config row is missing.

    None is not zero and must not be coerced to one. A store with no config row
    refuses every deletion (migration 137's ``COALESCE(..., 1000000000)`` makes
    that concrete), so reporting the absence as a number would describe a
    permissive floor where the real behaviour is total refusal.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()
    row = db.fetchone(
        "SELECT min_deletable_rubric_version AS v FROM distill_config WHERE id = 1"
    )
    return int(row["v"]) if row and row["v"] is not None else None


def rubric_versions_in_use(db=None) -> dict:
    """Which rubric versions the ledger and the playbook actually contain.

    The input to a floor decision: raising the floor above a version still
    carried by most of the ledger disqualifies that much of the corpus, and
    this is what makes the size of that consequence visible BEFORE the call
    rather than after it.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    ledger = {
        int(r["rubric_version"]): int(r["n"])
        for r in db.fetchall(
            "SELECT rubric_version, COUNT(*) AS n FROM session_distillations "
            "GROUP BY rubric_version ORDER BY rubric_version"
        )
    }
    lessons = {
        int(r["rubric_version"]): int(r["n"])
        for r in db.fetchall(
            "SELECT rubric_version, COUNT(*) AS n FROM distill_lessons "
            "GROUP BY rubric_version ORDER BY rubric_version"
        )
    }
    return {
        "current": current_rubric_version(),
        "floor": min_deletable_rubric_version(db),
        "ledger_rows_by_version": ledger,
        "lessons_by_version": lessons,
    }


def set_min_deletable_rubric_version(version: int, *, approved_by: str,
                                     allow_lowering: bool = False,
                                     db=None) -> dict:
    """Move the deletion floor. HUMAN CALL ONLY — nothing in okuro invokes this.

    Four refusals, and each closes a different way the floor could move without
    anybody deciding it should:

    ``approved_by`` must name a person. An empty or automation-shaped approver
    is the exact shape the switch exists to prevent, so it is refused rather
    than recorded.

    ``version`` may not exceed :func:`current_rubric_version` — a floor above
    any rubric that has ever run disqualifies the entire corpus and stops
    deletion permanently, which is the failure migration 136 documents.

    LOWERING requires ``allow_lowering=True``. Lowering re-permits deleting
    transcripts distilled under a rubric somebody previously distrusted; it is
    the only direction that WIDENS destruction, so it takes a second explicit
    argument rather than sharing the raising path.

    The config row must already exist. Creating it here would let a caller
    against a fresh store conjure a permissive floor where the schema's
    ``COALESCE`` default is total refusal.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    approver = str(approved_by or "").strip()
    if approver.lower() in _NON_HUMAN_APPROVERS:
        raise ValueError(
            f"min_deletable_rubric_version needs a human approver, got "
            f"{approved_by!r}. This switch exists because a PERSON decided an "
            f"older rubric is no longer trustworthy; no daemon may make that "
            f"call."
        )

    target = int(version)
    current = current_rubric_version()
    if target > current:
        raise ValueError(
            f"floor {target} exceeds the current rubric version {current} — "
            f"every distillation would be disqualified and deletion would stop "
            f"permanently (migration 136 documents this failure)."
        )
    if target < 1:
        raise ValueError(f"rubric versions start at 1, got {target}")

    row = db.fetchone(
        "SELECT min_deletable_rubric_version AS v FROM distill_config WHERE id = 1"
    )
    if row is None:
        raise ValueError(
            "distill_config has no row — refusing to create one here. A store "
            "without config refuses every deletion by design; writing a floor "
            "into it would turn that refusal into permission."
        )

    previous = int(row["v"]) if row["v"] is not None else None
    if previous is not None and target < previous and not allow_lowering:
        raise ValueError(
            f"lowering the floor {previous} -> {target} re-permits deleting "
            f"transcripts distilled under a rubric that was distrusted. Pass "
            f"allow_lowering=True to say that is intended."
        )

    with db.write():
        db.execute(
            "UPDATE distill_config SET min_deletable_rubric_version = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (target,),
        )

    log.warning(
        "distill: min_deletable_rubric_version %s -> %s, approved_by=%s",
        previous, target, approver,
    )
    return {"previous": previous, "current": target, "approved_by": approver}


def sessions_needing_redistillation(db=None, *, lesson_id: int | None = None,
                                    cluster_id: int | None = None,
                                    below_rubric_version: int | None = None,
                                    limit: int = 1000) -> list[str]:
    """Which sessions a rubric change implicates. SELECTS ONLY — never runs.

    Three ways to name the population, combined with AND:

    ``lesson_id``   the cluster that produced the lesson. A lesson mined under
                    an old rubric is a claim about sessions read under that
                    rubric, so both move together.
    ``cluster_id``  a cluster directly.
    ``below_rubric_version``  ledger rows stamped under an older rubric.
                    Defaults to the live floor, which is the version that
                    actually governs deletion — not the current rubric, since
                    being below the newest is not by itself a reason to redo
                    work (that conflation is what makes deletion stop forever).

    Returns ids. The caller decides whether to spend the tokens.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    clauses: list[str] = []
    params: list = []

    if lesson_id is not None:
        row = db.fetchone(
            "SELECT cluster_id FROM distill_lessons WHERE id = ?", (lesson_id,)
        )
        if row is None or row["cluster_id"] is None:
            return []
        clauses.append("f.cluster_id = ?")
        params.append(int(row["cluster_id"]))

    if cluster_id is not None:
        clauses.append("f.cluster_id = ?")
        params.append(int(cluster_id))

    floor = below_rubric_version
    if floor is None:
        floor = min_deletable_rubric_version(db)
    if floor is not None:
        # LEFT JOIN, so a session with NO ledger row qualifies too: never
        # distilled is "below every version", and an INNER JOIN here would
        # silently return only the sessions that HAVE been judged — the
        # opposite of the set a re-distillation pass wants.
        clauses.append("(d.session_id IS NULL OR d.rubric_version < ?)")
        params.append(int(floor))

    if not clauses:
        return []

    params.append(int(limit))
    return [
        r["session_id"]
        for r in db.fetchall(
            f"""
            SELECT f.session_id
            FROM distill_facets f
            LEFT JOIN session_distillations d ON d.session_id = f.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY f.session_id
            LIMIT ?
            """,
            tuple(params),
        )
    ]
