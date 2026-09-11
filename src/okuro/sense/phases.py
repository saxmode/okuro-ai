# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Declared phase plan per project — the queryable answer to "how far along".
# index:
#   imports
#   PHASE_STATES
#   def read_phases
#   def phase_summary
#   def project_phases_set
#   def touch_phase
# AGENT_HEADER_END -->
"""Declared phase plan per project — the queryable answer to "how far along".

Before this, "P1 done, P2-P7 not started" lived only inside free-text progress
summaries, so the question could only be answered by reading prose. Prose is
exactly what goes stale without anyone noticing.

THE SPLIT THAT KEEPS IT HONEST — declared plan, derived position:

  DECLARED, rarely   the phase LIST (key, order, title). A plan changes when
                     the plan changes, which is seldom. Rarely-written data
                     goes stale slowly.
  DERIVED, always    which phase is ACTIVE. ``log_progress(phase=...)``
                     promotes a ``pending`` phase to ``active`` as a side
                     effect of work the agent was already logging. No extra
                     discipline, so nothing to forget.
  DECLARED, once     ``done``. Deliberately NOT derived. "Finished" is a claim
                     about completeness that no activity signal can support —
                     a phase with recent commits may be half-built. Inferring
                     it is how a status view starts lying, which is worse than
                     having no status view.

A phase is referenced by KEY from ``progress.phase``. The keys are declared
here, so three sessions writing "P1", "phase 1" and "scoping" cannot each
invent their own label — an undeclared key is accepted (never break a caller)
but reported as ``undeclared`` by ``project_status`` rather than silently
counted.
"""

from __future__ import annotations

from typing import Any

PHASE_STATES = ("pending", "active", "done", "dropped")

# Terminal states — a phase here is never auto-promoted by activity. Promotion
# out of them is an explicit act, because "done" and "dropped" are claims a
# later progress row must not silently revoke.
_TERMINAL = ("done", "dropped")


def read_phases(project: str, db=None) -> list[dict[str, Any]]:
    """The declared plan for ``project``, in declared order. READ-ONLY."""
    if db is None:
        from okuro.db import get_db
        db = get_db()
    try:
        rows = db.fetchall(
            "SELECT project, key, seq, title, state, note, created_at, updated_at "
            "FROM project_phases WHERE project = ? ORDER BY seq, key",
            (project,),
        )
    except Exception:
        # Table absent — migration 130 has not run in this database yet. An
        # unmigrated store must degrade to "no plan declared", never raise:
        # project_status is a read tool and a read tool that dies on an old
        # schema is worse than one that reports an empty plan.
        return []
    return [dict(r) for r in rows]


def phase_summary(phases: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts + completion ratio over a declared plan.

    ``percent_done`` counts ``done`` against phases that are still in scope
    (``dropped`` is excluded from the denominator — a phase declared out of
    scope should not permanently cap a project below 100%).
    """
    counts = {s: 0 for s in PHASE_STATES}
    for p in phases:
        state = p.get("state") or "pending"
        counts[state] = counts.get(state, 0) + 1
    in_scope = len(phases) - counts.get("dropped", 0)
    percent = round(100.0 * counts.get("done", 0) / in_scope, 1) if in_scope else None
    active = [p["key"] for p in phases if p.get("state") == "active"]
    nxt = next(
        (p["key"] for p in phases if p.get("state") == "pending"),
        None,
    )
    return {
        "declared": len(phases),
        "in_scope": in_scope,
        "counts": counts,
        "percent_done": percent,
        "active": active,
        "next_pending": nxt,
    }


def project_phases_set(
    project: str,
    phases: list[dict[str, Any]],
    replace: bool = False,
) -> dict[str, Any]:
    """Declare or update the phase plan for ``project``. UPSERT by key.

    One tool does both jobs because they are the same write:

      declare a plan   ``phases=[{key,title,seq}, ...], replace=True``
      advance one      ``phases=[{"key": "p2", "state": "done"}]``

    Only the fields present in a dict are written, so advancing a phase cannot
    accidentally blank its title. ``replace=True`` drops phases absent from the
    list — the only way to remove one, and deliberately opt-in.

    ``seq`` defaults to list position when omitted on a fresh declaration, so
    the common case (an ordered list) needs no manual numbering.
    """
    from okuro.db import get_db

    if not project or not str(project).strip():
        return {"error": "project is required"}
    if not isinstance(phases, list) or not phases:
        return {"error": "phases must be a non-empty list of {key, ...} dicts"}

    normalised: list[dict[str, Any]] = []
    for i, raw in enumerate(phases):
        if not isinstance(raw, dict):
            return {"error": f"phases[{i}] is not an object: {raw!r}"}
        key = str(raw.get("key") or "").strip()
        if not key:
            return {"error": f"phases[{i}] has no 'key'"}
        state = raw.get("state")
        if state is not None and state not in PHASE_STATES:
            return {
                "error": (
                    f"phases[{i}] state={state!r} is not one of "
                    f"{', '.join(PHASE_STATES)}"
                )
            }
        normalised.append({
            "key": key,
            "seq": raw.get("seq", i),
            "title": raw.get("title"),
            "state": state,
            "note": raw.get("note"),
            "_seq_given": "seq" in raw,
        })

    db = get_db()
    try:
        existing = {r["key"]: dict(r) for r in read_phases(project, db)}
    except Exception as exc:  # pragma: no cover — surfaced, never swallowed
        return {"error": f"cannot read project_phases: {exc}"}

    created, updated, removed = [], [], []
    with db.write():
        # Register the target so phases are never declared against a slug no
        # project owns — the same failure _ensure_project guards for progress.
        from okuro.sense.progress import _ensure_project
        _ensure_project(db, project)

        for p in normalised:
            key = p["key"]
            if key in existing:
                sets, params = [], []
                if p["_seq_given"]:
                    sets.append("seq = ?")
                    params.append(int(p["seq"]))
                for col in ("title", "state", "note"):
                    if p[col] is not None:
                        sets.append(f"{col} = ?")
                        params.append(p[col])
                if not sets:
                    continue
                sets.append("updated_at = datetime('now')")
                params.extend([project, key])
                db.execute(
                    f"UPDATE project_phases SET {', '.join(sets)} "
                    f"WHERE project = ? AND key = ?",
                    tuple(params),
                )
                updated.append(key)
            else:
                db.execute(
                    "INSERT INTO project_phases (project, key, seq, title, state, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project, key, int(p["seq"]), p["title"],
                     p["state"] or "pending", p["note"]),
                )
                created.append(key)

        if replace:
            keep = {p["key"] for p in normalised}
            for key in existing:
                if key not in keep:
                    db.execute(
                        "DELETE FROM project_phases WHERE project = ? AND key = ?",
                        (project, key),
                    )
                    removed.append(key)

    plan = read_phases(project, db)
    return {
        "project": project,
        "created": created,
        "updated": updated,
        "removed": removed,
        "phases": plan,
        "summary": phase_summary(plan),
    }


def touch_phase(db, project: str, key: str) -> str | None:
    """Promote a ``pending`` phase to ``active``. Called from log_progress.

    This is the DERIVED half of the model and the reason the status does not
    rot: the agent logging progress already had to name what it worked on, so
    the position within the plan updates itself with no extra discipline —
    and provider compliance for a second, separate "advance the phase" call
    would be exactly as bad as it is for progress itself (453 of 2684 sessions).

    Returns the action taken, or ``None`` when the phase is not declared.
    Never demotes and never promotes out of ``done``/``dropped``: a later
    progress row must not silently revoke a completion claim.
    """
    if not key:
        return None
    try:
        row = db.fetchone(
            "SELECT state FROM project_phases WHERE project = ? AND key = ?",
            (project, key),
        )
    except Exception:
        return None  # unmigrated store
    if row is None:
        return "undeclared"
    state = row["state"]
    if state in _TERMINAL or state == "active":
        return "unchanged"
    db.execute(
        "UPDATE project_phases SET state = 'active', updated_at = datetime('now') "
        "WHERE project = ? AND key = ?",
        (project, key),
    )
    return "activated"
