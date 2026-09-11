# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Todos — actionable items registry (distinct from thoughts/memories).
# index:
#   def _row_to_dict
#   def _normalized_title
#   def _find_active_duplicate
#   def todo_add
#   def todo_list
#   def todo_get
#   def todo_update
#   def todo_done
#   def todo_delete
# AGENT_HEADER_END -->
"""Todos — actionable items the user or an agent has registered.

Backed by the ``todos`` table (schema: id, title, detail, status∈(open,
doing,done,dropped), priority 1-5, project, due_at, reminder_id, source
∈(user,agent,system,ingress), source_event_id, context(JSON), created_at,
updated_at, completed_at).

Agents should reach for this when they discover an actionable item that
needs the user's attention later — not a persistent learning (→
write_memory), not an ephemeral idea for the user to revisit
(→ capture_thought), not a time-scheduled nudge (→ set_reminder).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _existing_context(todo_id: str) -> dict:
    """The todo's current context dict, or {} — so a disposition stamp merges
    into it rather than wiping the caller's earlier context."""
    from okuro.db import get_db
    row = get_db().fetchone("SELECT context FROM todos WHERE id = ?", (todo_id,))
    if not row or not row["context"]:
        return {}
    try:
        data = json.loads(row["context"])
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


_VALID_STATUS = {"open", "doing", "done", "dropped"}
_VALID_SOURCE = {"user", "agent", "system", "ingress"}


def _row_to_dict(row: dict) -> dict:
    """Parse JSON columns into Python dicts for serialisation."""
    out = dict(row)
    raw = out.get("context")
    if isinstance(raw, str):
        try:
            out["context"] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            out["context"] = {}
    elif raw is None:
        out["context"] = {}
    return out


_DETAIL_PREVIEW_CHARS = 120


def _compact_row(row: dict) -> dict:
    """Scan-sized view of a todo: identity, state, and a detail preview.

    Drops the provenance blob entirely and truncates detail. Callers that
    need the rest drill in with todo_get.
    """
    out = dict(row)
    detail = out.get("detail")
    if isinstance(detail, str) and len(detail) > _DETAIL_PREVIEW_CHARS:
        out["detail"] = detail[:_DETAIL_PREVIEW_CHARS].rstrip() + "…"
    return out


def _normalized_title(title: str) -> str:
    """Title collapsed to a comparison key — case, punctuation, whitespace.

    Absorbs only churn that cannot change meaning, so "Ship the deck." and
    "Ship the deck" are one item while "Ship the deck" and "Send the deck"
    stay two. Mirrors notes_extract.extractor._normalize; kept local rather
    than imported so the todos leaf does not depend on an intake module.
    """
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (title or "").lower())).strip()


def _find_active_duplicate(db, title: str, project: Optional[str],
                           source_event_id: Optional[str]) -> Optional[str]:
    """Id of an existing OPEN/DOING todo this add would duplicate, or None.

    Exact signals only (DP12 — no fuzzy merge of distinct commitments):
      1. same source_event_id — a re-ingestion of the same origin event.
      2. else same title AND same project — a re-add of the same item, where
         "same title" ignores case/punctuation/whitespace but nothing else.
    A NULL project matches only NULL project (IS), never a real slug.

    The normalisation in (2) is deliberately not fuzzy. Re-ingestion produces
    titles that differ by a trailing period or a capital, and comparing raw
    strings let those through as separate todos; resemblance matching would
    instead merge two genuinely different commitments, and a swallowed item is
    a silent loss where a duplicate is a visible one.
    """
    if source_event_id:
        row = db.fetchone(
            "SELECT id FROM todos WHERE source_event_id = ? "
            "AND status IN ('open','doing') LIMIT 1",
            (source_event_id,),
        )
        if row:
            return row["id"]

    # Compared in Python: SQLite has no regex, and the normalisation must match
    # the one above exactly. Scoped to one project's active set, so the scan is
    # small and todo_add stays a single-digit-millisecond call.
    if project is None:
        rows = db.fetchall(
            "SELECT id, title FROM todos WHERE project IS NULL "
            "AND status IN ('open','doing')",
        )
    else:
        rows = db.fetchall(
            "SELECT id, title FROM todos WHERE project = ? "
            "AND status IN ('open','doing')",
            (project,),
        )
    target = _normalized_title(title)
    if not target:
        return None
    for row in rows:
        if _normalized_title(row["title"]) == target:
            return row["id"]
    return None


def todo_add(
    title: str,
    detail: Optional[str] = None,
    priority: int = 3,
    project: Optional[str] = None,
    due_at: Optional[str] = None,
    source: str = "agent",
    source_event_id: Optional[str] = None,
    context: Optional[dict] = None,
    reminder_id: Optional[str] = None,
) -> dict:
    """Register an actionable item.

    Args:
        title: One-line imperative (e.g. "Review Q2 vendor list").
        detail: Optional multi-line context the user (or a follow-up agent)
            will need when picking up the todo.
        priority: 1 (low) .. 5 (critical). Defaults to 3.
        project: Project slug (see list_projects). Optional but recommended.
        due_at: ISO-8601 timestamp when this should be completed.
        source: Who/what registered it — 'user', 'agent' (default),
            'system', or 'ingress' (e.g. obsidian vault scan).
        source_event_id: Optional anchor to the originating event for
            dedup on re-ingestion.
        context: Free-form JSON payload (links, thought_id, etc.).
        reminder_id: If linked to an existing reminder, its id.

    Returns:
        dict with the full inserted row (including generated id).

    Raises:
        ValueError if priority/status/source are out of range.
    """
    if not title or not title.strip():
        raise ValueError("title must be non-empty")
    if not 1 <= int(priority) <= 5:
        raise ValueError("priority must be 1..5")
    if source not in _VALID_SOURCE:
        raise ValueError(f"source must be one of {sorted(_VALID_SOURCE)}")

    from okuro.db import get_db

    db = get_db()

    # Inherit the project before the dedup pass — _find_active_duplicate below
    # matches on exact title+project, so resolving after it would compare on
    # NULL and then insert a slug, defeating the dedup it is meant to feed.
    #
    # NOT for source='ingress'. Ingress writes are produced by the daemon from
    # external material (a vault scan, a saved link) and its executor already
    # resolves and VALIDATES a slug of its own, deliberately passing None when
    # the material names no project. Inheriting a session's project there would
    # attribute someone else's ingested item to whichever session happened to
    # be running — the 682 tiktok-saves rows are exactly this population.
    if source != "ingress":
        from okuro.sense.progress import resolve_write_project

        project = resolve_write_project(db, project)

    # Dedup on ingress — the source fix for todo accretion (P0-4). Every add
    # minted a fresh row, so a re-ingested vault note or a re-run scanner
    # created a NEW todo each time; the store reached 1236 open / 26 done with
    # visible duplicate pairs. source_event_id was documented as the dedup key
    # and never checked. Match only ACTIVE (open/doing) rows so a genuinely
    # recurring task can be re-added once its prior instance is closed, and only
    # on EXACT signals (event id, else exact title+project) so dedup never
    # false-merges two distinct todos — a wrongly-merged commitment is the same
    # silent loss as a dropped one.
    existing = _find_active_duplicate(db, title.strip(), project, source_event_id)
    if existing:
        return todo_get(existing)

    todo_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO todos (
            id, title, detail, status, priority, project, due_at,
            reminder_id, source, source_event_id, context
        ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            todo_id,
            title.strip(),
            detail,
            int(priority),
            project,
            due_at,
            reminder_id,
            source,
            source_event_id,
            json.dumps(context or {}),
        ),
    )
    return todo_get(todo_id)


def todo_list(
    status: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
    verbose: bool = False,
) -> list[dict]:
    """List todos with optional filters.

    Compact by default: enough to scan and pick, not enough to drown in.
    Ingress-scanned todos carry a provenance blob (scanner, source note,
    entities, rationale) that is large and rarely relevant while scanning
    a list — a single project's active set can exceed an MCP client's
    token ceiling and fail the call outright. Drill into one todo with
    todo_get(todo_id) to get everything.

    Args:
        status: One of 'open','doing','done','dropped'. If None, returns
            the active set (open + doing).
        project: Restrict to one project slug.
        limit: Max rows to return.
        verbose: Return the full row (detail untruncated, plus context,
            source_event_id, claimed_session_id, reminder_id, timestamps).
            Callers with no token budget concern — the HTTP API — pass True.

    Returns:
        List of dicts ordered by priority DESC, due_at ASC (nulls last),
        created_at DESC.
    """
    clauses: list[str] = []
    params: list = []

    if status:
        if status not in _VALID_STATUS:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUS)}")
        clauses.append("status = ?")
        params.append(status)
    else:
        clauses.append("status IN ('open', 'doing')")

    # Defense-in-depth: never surface legacy inline-session stub todos
    # (id `stream-stub-*`). The producer no longer mints them and mig 060
    # soft-reaps existing ones — this guards any that slip through.
    clauses.append("id NOT LIKE 'stream-stub-%'")

    if project:
        clauses.append("project = ?")
        params.append(project)

    where = " AND ".join(clauses)
    params.append(int(limit))

    from okuro.db import get_db

    columns = (
        """id, title, detail, status, priority, project, due_at,
               reminder_id, source, source_event_id, context,
               claimed_session_id,
               created_at, updated_at, completed_at"""
        if verbose
        else "id, title, detail, status, priority, project, due_at, source"
    )

    db = get_db()
    rows = db.fetchall(
        f"""
        SELECT {columns}
          FROM todos
         WHERE {where}
         ORDER BY priority DESC,
                  CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                  due_at ASC,
                  created_at DESC
         LIMIT ?
        """,
        tuple(params),
    )
    if verbose:
        return [_row_to_dict(r) for r in rows]
    return [_compact_row(r) for r in rows]


def todo_get(todo_id: str) -> dict:
    """Return one todo by id, or raise KeyError."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        """
        SELECT id, title, detail, status, priority, project, due_at,
               reminder_id, source, source_event_id, context,
               claimed_session_id,
               created_at, updated_at, completed_at
          FROM todos
         WHERE id = ?
        """,
        (todo_id,),
    )
    if row is None:
        raise KeyError(f"todo {todo_id!r} not found")
    return _row_to_dict(row)


def todo_update(
    todo_id: str,
    title: Optional[str] = None,
    detail: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    project: Optional[str] = None,
    due_at: Optional[str] = None,
    reminder_id: Optional[str] = None,
    context: Optional[dict] = None,
    reason: Optional[str] = None,
) -> dict:
    """Patch a todo. Only non-None args are applied.

    Setting status to 'done' stamps completed_at automatically.

    ``reason`` records WHY on a terminal transition (dropped/done). A drop with
    no reason is the silent-loss the disposition work exists to end — you would
    never know what vanished or why (P0-4). The reason is stamped into the
    todo's context under a `disposition` record (status, reason, at); dropping
    with no reason still records that none was given, so the absence is itself
    visible rather than invisible. No schema change — it rides the context JSON.
    """
    fields: list[str] = []
    params: list = []

    # A terminal transition records its disposition into context, merged with
    # whatever context the caller also passed so neither clobbers the other.
    if status in ("dropped", "done"):
        _disp = dict(context) if context else _existing_context(todo_id)
        _disp["disposition"] = {
            "status": status,
            "reason": (reason or "").strip() or None,
            "at": _utc_now_iso(),
        }
        context = _disp

    if title is not None:
        if not title.strip():
            raise ValueError("title must be non-empty")
        fields.append("title = ?")
        params.append(title.strip())
    if detail is not None:
        fields.append("detail = ?")
        params.append(detail)
    if status is not None:
        if status not in _VALID_STATUS:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUS)}")
        fields.append("status = ?")
        params.append(status)
        if status == "done":
            fields.append("completed_at = datetime('now')")
        else:
            fields.append("completed_at = NULL")
    if priority is not None:
        if not 1 <= int(priority) <= 5:
            raise ValueError("priority must be 1..5")
        fields.append("priority = ?")
        params.append(int(priority))
    if project is not None:
        fields.append("project = ?")
        params.append(project)
    if due_at is not None:
        fields.append("due_at = ?")
        params.append(due_at)
    if reminder_id is not None:
        fields.append("reminder_id = ?")
        params.append(reminder_id)
    if context is not None:
        fields.append("context = ?")
        params.append(json.dumps(context))

    if not fields:
        return todo_get(todo_id)

    fields.append("updated_at = datetime('now')")
    params.append(todo_id)

    from okuro.db import get_db

    db = get_db()
    cur = db.execute(
        f"UPDATE todos SET {', '.join(fields)} WHERE id = ?",
        tuple(params),
    )
    if cur.rowcount == 0:
        raise KeyError(f"todo {todo_id!r} not found")
    return todo_get(todo_id)


def todos_needing_review(older_than_days: int = 30, limit: int = 5,
                         project: Optional[str] = None) -> list[dict]:
    """Oldest OPEN/DOING todos untouched for ``older_than_days``, oldest first.

    The disposition surface for the todo backlog (P0-4). The store reached 1236
    open / 26 done; 403 items sat 30-90d old. Bulk-expiring by age is explicitly
    wrong — that band holds real commitments to real people, and silent deletion
    is the same failure as a dropped write nobody sees. Instead a SMALL batch of
    the stalest items is surfaced each session for a human keep-or-drop call, so
    the backlog drains by decision, gradually, never by a timer.

    Freshness is `updated_at` — todo_add stamps it and todo_update bumps it, so a
    todo that has been touched (re-prioritised, re-scoped, worked) is not stale.
    """
    from okuro.db import get_db

    db = get_db()
    clauses = ["status IN ('open','doing')",
               "id NOT LIKE 'stream-stub-%'",
               "julianday('now') - julianday(updated_at) >= ?"]
    params: list = [int(older_than_days)]
    if project:
        clauses.append("project = ?")
        params.append(project)
    params.append(int(limit))
    rows = db.fetchall(
        "SELECT id, title, detail, status, priority, project, due_at, "
        "reminder_id, source, source_event_id, context, claimed_session_id, "
        "created_at, updated_at, completed_at "
        f"FROM todos WHERE {' AND '.join(clauses)} "
        "ORDER BY updated_at ASC LIMIT ?",
        tuple(params),
    )
    return [_row_to_dict(r) for r in rows]


def todo_done(todo_id: str) -> dict:
    """Mark a todo done. Equivalent to todo_update(id, status='done')."""
    return todo_update(todo_id, status="done")


def todo_delete(todo_id: str) -> dict:
    """Hard-delete a todo. For soft-delete, use todo_update(status='dropped')."""
    from okuro.db import get_db

    db = get_db()
    cur = db.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    if cur.rowcount == 0:
        raise KeyError(f"todo {todo_id!r} not found")
    return {"id": todo_id, "deleted": True}
