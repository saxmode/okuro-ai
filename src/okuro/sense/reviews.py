# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Contextual reviews on any okuro surface — append-only, with
#   severity-gated todo assembly and a tombstoning surface registry.
# index:
#   SEVERITIES | TARGET_TYPES | TODO_SEVERITIES
#   def review_add
#   def review_list
#   def surface_summary
#   def register_surfaces
#   def _assemble_todo
# AGENT_HEADER_END -->
"""Reviews — "write down, in context, what I don't like".

Backed by ``reviews`` + ``review_surfaces`` (migration 111).

IDENTITY vs CONTEXT is the whole design. ``surface_id`` is a DECLARED name and
is the only thing used to identify what was reviewed. Route, params, viewport
and DOM hints are recorded as evidence and never as identity — every derivable
candidate rots, and this app has already renamed ``/tasks`` -> ``/work``,
``/roles`` -> ``/agents``, ``/dashboard`` -> ``/agents``, ``/system`` ->
``/health``.

Distinct from :mod:`okuro.orchestrator.feedback` (``flow_feedback``, migration
108) on purpose. That table is one row per task and a re-rate REPLACEs it,
which is correct for "how did this flow go" and is depended on by the learning
join and the forward-note queue. This module answers a different question —
"what does he think of this surface, over time" — so it is append-only and
lives in its own table. The task-detail card writes both.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

logger = logging.getLogger("okuro.sense.reviews")

SEVERITIES = ("blocker", "annoyance", "idea", "praise")

# Which severities create a todo. Deterministic and decided at WRITE time, not
# by an async classifier: the todo store previously accreted to 1236 open / 26
# done with visible duplicates (todos.py:169-172), and "every review becomes a
# todo" would recreate that at higher volume. An idea or a compliment is worth
# recording and worth reading later; it is not worth a task on a backlog.
TODO_SEVERITIES = ("blocker", "annoyance")

# Mirrors the CHECK in migration 111, which follows note_links (071:42-50).
TARGET_TYPES = (
    "task", "subtask", "note", "person", "project", "thought",
    "artifact", "role", "deck", "kg", "unresolved",
)

_SEVERITY_PRIORITY = {"blocker": 5, "annoyance": 3}


def _assemble_todo(
    review_id: str,
    surface_id: str,
    comment: str,
    severity: str,
    label: Optional[str],
    project: Optional[str],
) -> Optional[str]:
    """Create the todo for a review, or None if there is nothing to write.

    The severity GATE lives at the single call site in :func:`review_add`, not
    here — duplicating it meant an explicit ``make_todo=True`` was silently
    re-declined by this function. This one only refuses what it cannot write:
    a todo whose title would be empty.

    ``source_event_id`` is ALWAYS the review id. todo_add dedups on that first
    and falls back to an exact normalised title match against every open todo
    in the project (todos.py:87-117) — so without it, two reviews carrying the
    same phrasing about DIFFERENT surfaces would silently collapse into one
    todo and the second complaint would vanish.
    """
    if not (comment or "").strip():
        return None
    try:
        from okuro.sense.todos import todo_add
        where = label or surface_id
        row = todo_add(
            title=f"{where}: {comment.strip()[:120]}",
            detail=(
                f"From a review of `{surface_id}`.\n\n"
                f"Severity: {severity}\n\n{comment.strip()}"
            ),
            priority=_SEVERITY_PRIORITY.get(severity, 3),
            project=project,
            source="user",
            source_event_id=review_id,
            context={"review_id": review_id, "surface_id": surface_id},
        )
        return (row or {}).get("id")
    except Exception as exc:  # noqa: BLE001
        # A failed todo must never lose the review — the review IS the record.
        logger.warning("review %s: todo assembly failed (%r)", review_id, exc)
        return None


def review_add(
    surface_id: str,
    *,
    comment: Optional[str] = None,
    rating: Optional[int] = None,
    severity: str = "annoyance",
    target_type: str = "unresolved",
    target_id: Optional[str] = None,
    route: Optional[str] = None,
    route_params: Optional[dict] = None,
    viewport: Optional[str] = None,
    app_version: Optional[str] = None,
    dom_hint: Optional[str] = None,
    screenshot_ref: Optional[str] = None,
    project: Optional[str] = None,
    make_todo: Optional[bool] = None,
) -> dict:
    """Append one review. Never updates a prior row — history is the point.

    ``make_todo`` overrides the severity gate in both directions (the UI shows
    the gate's decision as a toggle so it is visible and correctable). ``None``
    means "use the gate".

    Returns the inserted row, including ``todo_id`` when one was created.
    """
    if not surface_id or not surface_id.strip():
        raise ValueError("surface_id must be non-empty")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    if target_type not in TARGET_TYPES:
        raise ValueError(f"target_type must be one of {TARGET_TYPES}")
    if rating is not None and not 1 <= int(rating) <= 5:
        raise ValueError("rating must be 1..5 or None")
    if not (comment or "").strip() and rating is None:
        # A review with neither a score nor words records nothing.
        raise ValueError("a review needs a comment, a rating, or both")

    from okuro.db import get_db

    db = get_db()
    review_id = str(uuid.uuid4())
    surface_id = surface_id.strip()

    db.execute(
        "INSERT INTO reviews (id, surface_id, rating, comment, severity, "
        "target_type, target_id, route, route_params, viewport, app_version, "
        "dom_hint, screenshot_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            review_id, surface_id, rating, (comment or None), severity,
            target_type, target_id, route,
            json.dumps(route_params) if route_params else None,
            viewport, app_version, dom_hint, screenshot_ref,
        ),
    )

    # Keep the registry honest even for a surface that never announced itself,
    # so a review can never point at an unknown id.
    _touch_surface(db, surface_id, route=route)

    should = (
        make_todo if make_todo is not None else (severity in TODO_SEVERITIES)
    )
    todo_id = None
    if should:
        todo_id = _assemble_todo(
            review_id, surface_id, comment or "", severity,
            _surface_label(db, surface_id), project,
        )
        if todo_id:
            db.execute(
                "UPDATE reviews SET todo_id = ? WHERE id = ?",
                (todo_id, review_id),
            )

    row = db.fetchone("SELECT * FROM reviews WHERE id = ?", (review_id,))
    out = dict(row) if row else {"id": review_id, "todo_id": todo_id}

    # Best-effort forward to the maintainer. LOCAL-FIRST: the row above is
    # already committed, so a disabled, misconfigured, offline or failing sync
    # costs nothing — the review stays queued (synced_at IS NULL) and a later
    # drain picks it up. Never let this raise into the caller.
    try:
        from okuro.sense.review_sync import sync_pending
        sync_pending(limit=25)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review %s: sync attempt failed (%r)", review_id, exc)

    return out


def _surface_label(db, surface_id: str) -> Optional[str]:
    row = db.fetchone(
        "SELECT label FROM review_surfaces WHERE surface_id = ?", (surface_id,)
    )
    return (dict(row).get("label") if row else None)


def _touch_surface(db, surface_id: str, *, route: Optional[str] = None) -> None:
    db.execute(
        "INSERT INTO review_surfaces (surface_id, route) VALUES (?, ?) "
        "ON CONFLICT(surface_id) DO UPDATE SET "
        "  last_seen_at = datetime('now'), "
        "  route = COALESCE(excluded.route, review_surfaces.route)",
        (surface_id, route),
    )


def review_list(
    surface_id: Optional[str] = None,
    *,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Reviews, newest first. Filter by surface and/or by what it was about."""
    from okuro.db import get_db

    where, params = [], []
    if surface_id:
        where.append("surface_id = ?")
        params.append(surface_id)
    if target_type:
        where.append("target_type = ?")
        params.append(target_type)
    if target_id:
        where.append("target_id = ?")
        params.append(target_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = get_db().fetchall(
        f"SELECT * FROM reviews {clause} ORDER BY created_at DESC, rowid DESC "
        f"LIMIT ?",
        (*params, int(limit)),
    )
    return [dict(r) for r in rows or []]


def surface_summary(*, include_orphaned: bool = False) -> list[dict]:
    """Per-surface rollup: how many reviews, the latest verdict, open todos.

    Orphaned surfaces are excluded by default — their reviews stay readable and
    their todos stay open, they simply do not count toward the health of what
    is on screen today.
    """
    from okuro.db import get_db

    clause = "" if include_orphaned else "WHERE s.orphaned_at IS NULL"
    rows = get_db().fetchall(
        "SELECT s.surface_id, s.label, s.route, s.orphaned_at, "
        "       COUNT(r.id) AS review_count, "
        "       MAX(r.created_at) AS last_review_at, "
        "       AVG(r.rating) AS avg_rating, "
        "       SUM(CASE WHEN r.severity = 'blocker' THEN 1 ELSE 0 END) AS blockers "
        "FROM review_surfaces s LEFT JOIN reviews r "
        "  ON r.surface_id = s.surface_id "
        f"{clause} "
        "GROUP BY s.surface_id ORDER BY blockers DESC, review_count DESC",
    )
    return [dict(r) for r in rows or []]


def register_surfaces(catalogue: list[dict]) -> dict:
    """Reconcile the live surface catalogue the frontend reports on boot.

    Anything present is upserted and un-orphaned; anything in the registry but
    absent from the catalogue is TOMBSTONED (``orphaned_at`` stamped) and never
    deleted — a removed surface's complaint may be exactly why it was removed,
    so its reviews stay readable and its todos stay open.

    Returns counts, so a caller can tell a real removal from an empty payload.
    """
    from okuro.db import get_db

    db = get_db()
    live: set[str] = set()
    for entry in catalogue or []:
        sid = str((entry or {}).get("surface_id") or "").strip()
        if not sid:
            continue
        live.add(sid)
        db.execute(
            "INSERT INTO review_surfaces (surface_id, label, route) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(surface_id) DO UPDATE SET "
            "  label = COALESCE(excluded.label, review_surfaces.label), "
            "  route = COALESCE(excluded.route, review_surfaces.route), "
            "  last_seen_at = datetime('now'), "
            "  orphaned_at = NULL",
            (sid, entry.get("label"), entry.get("route")),
        )

    # An empty catalogue means "the frontend told us nothing", not "every
    # surface is gone" — orphaning the whole registry on a failed boot would be
    # a self-inflicted outage of the health view.
    if not live:
        return {"registered": 0, "orphaned": 0, "skipped_empty": True}

    placeholders = ",".join("?" for _ in live)
    cur = db.execute(
        f"UPDATE review_surfaces SET orphaned_at = datetime('now') "
        f"WHERE orphaned_at IS NULL AND surface_id NOT IN ({placeholders})",
        tuple(live),
    )
    return {
        "registered": len(live),
        "orphaned": getattr(cur, "rowcount", 0) or 0,
        "skipped_empty": False,
    }
