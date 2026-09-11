# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Reminders API — list/create/edit/delete + snooze/dismiss/ack + suggestions.
# index:
#   imports
#   router
#   models
#   def _require_localhost_rm
#   def _parse_row
#   def list_reminders_endpoint
#   def create_reminder_endpoint
#   def update_reminder_endpoint
#   def delete_reminder_endpoint
#   def snooze_endpoint
#   def dismiss_endpoint
#   def acknowledge_endpoint
#   def list_suggestions_endpoint
#   def accept_suggestion_endpoint
#   def reject_suggestion_endpoint
# AGENT_HEADER_END -->
"""Reminders API — thin HTTP wrapper over okuro.sense.reminders.

Mirrors the MCP reminder tools:

- GET    /api/reminders                               list (filter by status + upcoming_hours)
- POST   /api/reminders                               create
- PUT    /api/reminders/{id}                          edit (localhost-only)
- DELETE /api/reminders/{id}                          remove (localhost-only)
- POST   /api/reminders/{id}/snooze                   body {duration_min?}
- POST   /api/reminders/{id}/dismiss
- POST   /api/reminders/{id}/acknowledge
- GET    /api/reminders/suggestions                   system-proposed pending suggestions
- POST   /api/reminders/suggestions/{id}/accept
- POST   /api/reminders/suggestions/{id}/reject

Time handling: server accepts ISO-8601 strings (with or without timezone),
normalises to UTC, and always returns ISO-8601 UTC strings. Clients should
display using their own locale + timezone (the profile's, never a hardcoded one).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.reminders")

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_rm(request: Request) -> None:
    """Loopback guard — same pattern as keyring/cortex/services/people."""
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)


def _parse_row(row) -> dict:
    """Parse a reminders row: JSON text columns → dicts / lists."""
    rec = dict(row)
    for col in ("context", "repeat"):
        val = rec.get(col)
        if isinstance(val, str):
            try:
                rec[col] = json.loads(val) if val else {}
            except (json.JSONDecodeError, TypeError):
                rec[col] = {}
    return rec


def _parse_suggestion(row) -> dict:
    """Parse a reminder_suggestions row."""
    return dict(row)


# ── Models ───────────────────────────────────────────────────────────


class ReminderCreate(BaseModel):
    what: str
    when_due: str  # ISO-8601
    urgency: int = Field(default=3, ge=1, le=5)
    channels: Optional[list[str]] = None
    repeat: Optional[dict] = None
    project: Optional[str] = None
    context: Optional[dict] = None


class ReminderUpdate(BaseModel):
    what: Optional[str] = None
    when_due: Optional[str] = None
    urgency: Optional[int] = Field(default=None, ge=1, le=5)
    status: Optional[str] = None  # pending|active|snoozed|done|dismissed
    repeat: Optional[dict] = None
    context: Optional[dict] = None


class SnoozeRequest(BaseModel):
    duration_min: Optional[int] = Field(default=None, ge=1, le=60 * 24 * 14)


class AcceptSuggestion(BaseModel):
    override_when: Optional[str] = None
    override_urgency: Optional[int] = Field(default=None, ge=1, le=5)


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("")
def list_reminders_endpoint(
    status: Optional[str] = Query(
        default=None, description="pending|active|snoozed|done|dismissed — default = open set"
    ),
    upcoming_hours: Optional[float] = Query(default=None, ge=0),
    include_suggestions: bool = Query(default=False),
) -> dict:
    """List reminders, matching ``list_reminders`` MCP tool semantics."""
    from okuro.sense.reminders.engine import list_reminders

    data = list_reminders(
        status=status,
        upcoming_hours=upcoming_hours,
        include_suggestions=include_suggestions,
    )
    # engine already parses JSON — but be defensive in case the shape drifts.
    reminders = [
        _parse_row(r) if isinstance(r.get("context"), str) else r for r in data.get("reminders", [])
    ]
    out: dict = {"reminders": reminders}
    if include_suggestions:
        out["suggestions"] = [_parse_suggestion(s) for s in data.get("suggestions", [])]
    return out


@router.post("", status_code=201)
def create_reminder_endpoint(payload: ReminderCreate, request: Request) -> dict:
    """Create a reminder (cascade is auto-generated from profile strategy)."""
    _require_localhost_rm(request)

    # Validate ISO-8601 up front so we get a clean 400 rather than a 500.
    try:
        dt = datetime.fromisoformat(payload.when_due.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid when_due (expected ISO-8601): {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    from okuro.sense.reminders.engine import create_reminder

    reminder = create_reminder(
        what=payload.what,
        when_due=dt,
        urgency=payload.urgency,
        channels=payload.channels,
        repeat=payload.repeat,
        project=payload.project,
        context=payload.context,
        source="user",
    )
    return reminder


@router.put("/{reminder_id}")
def update_reminder_endpoint(
    reminder_id: str, payload: ReminderUpdate, request: Request
) -> dict:
    """Patch reminder fields. If ``when_due`` changes the cascade is regenerated."""
    _require_localhost_rm(request)

    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    if not row:
        raise HTTPException(404, f"Reminder not found: {reminder_id}")
    current = _parse_row(row)

    new_due_str: Optional[str] = None
    regenerate_cascade = False
    if payload.when_due is not None:
        try:
            dt = datetime.fromisoformat(payload.when_due.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"Invalid when_due: {exc}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        new_due_str = dt.isoformat()
        regenerate_cascade = new_due_str != current.get("when_due")

    updates: list[str] = []
    params: list = []
    if payload.what is not None:
        updates.append("what = ?")
        params.append(payload.what)
    if new_due_str is not None:
        updates.append("when_due = ?")
        params.append(new_due_str)
    if payload.urgency is not None:
        updates.append("urgency = ?")
        params.append(payload.urgency)
    if payload.status is not None:
        if payload.status not in {"pending", "active", "snoozed", "done", "dismissed"}:
            raise HTTPException(400, f"Invalid status: {payload.status}")
        updates.append("status = ?")
        params.append(payload.status)
    if payload.repeat is not None:
        updates.append("repeat = ?")
        params.append(json.dumps(payload.repeat))
    if payload.context is not None:
        updates.append("context = ?")
        params.append(json.dumps(payload.context))

    if updates:
        updates.append("updated_at = datetime('now')")
        sql = f"UPDATE reminders SET {', '.join(updates)} WHERE id = ?"
        params.append(reminder_id)
        db.execute(sql, tuple(params))
        db.conn.commit()

    if regenerate_cascade and new_due_str:
        try:
            from okuro.sense.reminders.cascade import generate_cascade
            from okuro.sense.reminders.engine import _load_strategy

            new_dt = datetime.fromisoformat(new_due_str)
            # Wipe pending cascade and re-materialise.
            db.execute(
                "DELETE FROM reminder_cascade WHERE reminder_id = ? AND status = 'pending'",
                (reminder_id,),
            )
            db.conn.commit()
            generate_cascade(
                reminder_id=reminder_id,
                when_due=new_dt,
                urgency=payload.urgency or current.get("urgency", 3),
                strategy=_load_strategy(),
            )
        except Exception as exc:
            logger.warning("Cascade regen failed for %s: %s", reminder_id, exc)

    row = db.fetchone("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    return _parse_row(row)


@router.delete("/{reminder_id}", status_code=204, response_model=None)
def delete_reminder_endpoint(reminder_id: str, request: Request) -> None:
    """Hard delete — cascade steps go via FK ON DELETE CASCADE."""
    _require_localhost_rm(request)
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
    if not row:
        raise HTTPException(404, f"Reminder not found: {reminder_id}")
    db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    db.conn.commit()
    return None


@router.post("/{reminder_id}/snooze")
def snooze_endpoint(reminder_id: str, payload: SnoozeRequest, request: Request) -> dict:
    """Snooze — regenerates cascade via engine.snooze_reminder."""
    _require_localhost_rm(request)
    from okuro.db import get_db
    from okuro.sense.reminders.engine import snooze_reminder

    db = get_db()
    row = db.fetchone("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
    if not row:
        raise HTTPException(404, f"Reminder not found: {reminder_id}")
    return snooze_reminder(reminder_id=reminder_id, duration_min=payload.duration_min)


@router.post("/{reminder_id}/dismiss")
def dismiss_endpoint(reminder_id: str, request: Request) -> dict:
    """Dismiss — skips remaining cascade."""
    _require_localhost_rm(request)
    from okuro.db import get_db
    from okuro.sense.reminders.engine import dismiss_reminder

    db = get_db()
    row = db.fetchone("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
    if not row:
        raise HTTPException(404, f"Reminder not found: {reminder_id}")
    return dismiss_reminder(reminder_id)


@router.post("/{reminder_id}/acknowledge")
def acknowledge_endpoint(reminder_id: str, request: Request) -> dict:
    """Acknowledge — marks done, skips remaining cascade."""
    _require_localhost_rm(request)
    from okuro.db import get_db
    from okuro.sense.reminders.engine import acknowledge_reminder

    db = get_db()
    row = db.fetchone("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
    if not row:
        raise HTTPException(404, f"Reminder not found: {reminder_id}")
    return acknowledge_reminder(reminder_id)


# ── Suggestions ───────────────────────────────────────────────────────


@router.get("/suggestions")
def list_suggestions_endpoint() -> dict:
    """List pending (accepted IS NULL) reminder suggestions."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT * FROM reminder_suggestions WHERE accepted IS NULL ORDER BY created_at DESC"
    )
    return {"suggestions": [_parse_suggestion(r) for r in rows]}


@router.post("/suggestions/{suggestion_id}/accept")
def accept_suggestion_endpoint(
    suggestion_id: str, payload: AcceptSuggestion, request: Request
) -> dict:
    """Accept a suggestion → creates a reminder and links it back."""
    _require_localhost_rm(request)
    from okuro.sense.reminders.suggestions import accept_suggestion

    try:
        return accept_suggestion(
            suggestion_id=suggestion_id,
            override_when=payload.override_when,
            override_urgency=payload.override_urgency,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/suggestions/{suggestion_id}/reject")
def reject_suggestion_endpoint(suggestion_id: str, request: Request) -> dict:
    """Reject a suggestion (accepted=0)."""
    _require_localhost_rm(request)
    from okuro.sense.reminders.suggestions import reject_suggestion

    return reject_suggestion(suggestion_id)
