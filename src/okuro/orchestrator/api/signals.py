# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Signals REST API — thin wrapper over okuro.sense.signals.
# index:
#   imports
#   router
#   models
#   def list_signals_route
#   def create_signal_route
#   def get_signal_route
#   def promote_signal_route
#   def discard_signal_route
#   def ignore_signal_route
# AGENT_HEADER_END -->
"""Signals REST API — HTTP surface for the proactive-signals queue.

Mirrors the CRUD helpers in :mod:`okuro.sense.signals` so the Now-page
Signals section + future audit views can reach the queue via the same
global bearer used elsewhere in /api/.

Endpoints
---------
GET    /api/signals                   list (default: status='open', limit=50)
POST   /api/signals                   create (ingest path)
GET    /api/signals/{id}              single
POST   /api/signals/{id}/promote      promote → todos row
POST   /api/signals/{id}/discard      discard with reason
POST   /api/signals/{id}/ignore       mark ignored (expires)
POST   /api/signals/{id}/find-strategy continuation → orchestrator task

Auth: inherits the global ``BearerAuthMiddleware`` — no separate auth
surface like the per-session ``/api/sessions/`` mount. Wave 4c ingest
scripts use the same global token as the SPA.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from okuro.sense import signals as signals_svc

logger = logging.getLogger("okuro.orchestrator.api.signals")

router = APIRouter(prefix="/api/signals", tags=["signals"])


# ── Models ───────────────────────────────────────────────────────────


class SignalCreate(BaseModel):
    source: str
    severity: str
    summary: str
    source_ref: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    suggested_action: Optional[str] = None
    auto_promote: bool = False
    expires_at: Optional[str] = None


class PromoteBody(BaseModel):
    todo_title: Optional[str] = None
    todo_priority: int = Field(default=3, ge=1, le=5)
    project: Optional[str] = None


class DiscardBody(BaseModel):
    reason: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("")
def list_signals_route(
    status: Optional[str] = "open",
    limit: int = 50,
) -> dict:
    try:
        rows = signals_svc.signal_list(status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"signals": rows}


@router.post("", status_code=201)
def create_signal_route(body: SignalCreate) -> dict:
    try:
        return signals_svc.signal_add(**body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{signal_id}")
def get_signal_route(signal_id: str) -> dict:
    try:
        return signals_svc.signal_get(signal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")


@router.post("/{signal_id}/promote")
def promote_signal_route(signal_id: str, body: PromoteBody | None = None) -> dict:
    payload = body.model_dump(exclude_none=True) if body else {}
    try:
        return signals_svc.signal_promote(signal_id, **payload)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{signal_id}/discard")
def discard_signal_route(signal_id: str, body: DiscardBody | None = None) -> dict:
    reason = body.reason if body else None
    try:
        return signals_svc.signal_discard(signal_id, reason=reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{signal_id}/ignore")
def ignore_signal_route(signal_id: str) -> dict:
    try:
        return signals_svc.signal_ignore(signal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{signal_id}/find-strategy")
def find_strategy_route(signal_id: str) -> dict:
    """Hand a continuation signal off to the orchestrator.

    Builds a planning brief from the signal's invitation +
    blocking_step + evidence_refs, calls ``orchestrator_create`` in
    mode='plan' (so the user reviews before execute), then marks the
    signal as ``promoted`` so it leaves the open queue.

    Returns ``{signal_id, task_id, redirect_url, plan_summary}``.
    Rejects with 409 when the signal is not an open continuation.
    """
    try:
        sig = signals_svc.signal_get(signal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")

    if sig["status"] != "open":
        raise HTTPException(
            status_code=409,
            detail=f"signal {signal_id} is {sig['status']!r}, expected 'open'",
        )

    evidence = sig.get("evidence") or {}
    if evidence.get("bucket") != "continuation":
        raise HTTPException(
            status_code=409,
            detail="find-strategy is only valid for bucket='continuation' signals",
        )

    description = _build_strategy_brief(sig, evidence)

    from okuro.sense.mcp_tools import orchestrator_create

    result = orchestrator_create(description=description, mode="plan")
    if "error" in result:
        raise HTTPException(
            status_code=502, detail=f"orchestrator_create failed: {result['error']}"
        )

    task_id = result.get("task_id")
    if not task_id:
        raise HTTPException(
            status_code=502, detail="orchestrator returned no task_id"
        )

    # Mark the signal as promoted so the row leaves the open queue.
    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE signals SET status = 'promoted' WHERE id = ?",
        (signal_id,),
    )

    return {
        "signal_id": signal_id,
        "task_id": task_id,
        "redirect_url": f"/work/{task_id}",
        "plan_summary": result.get("plan_summary"),
    }


def _build_strategy_brief(sig: dict, evidence: dict) -> str:
    """Compose the orchestrator task description from continuation evidence."""
    parts: list[str] = []
    project = evidence.get("source_id")
    if project:
        parts.append(f"Project: {project}")

    readiness = evidence.get("readiness_percent")
    if readiness is not None:
        parts.append(f"Readiness: {readiness}%")

    invitation = (sig.get("summary") or "").strip()
    if invitation:
        parts.append("")
        parts.append("## Invitation")
        parts.append(invitation)

    blocking = evidence.get("blocking_step") or sig.get("suggested_action")
    if blocking:
        parts.append("")
        parts.append("## Blocking step")
        parts.append(str(blocking).strip())

    refs = evidence.get("evidence_refs") or []
    if isinstance(refs, list) and refs:
        parts.append("")
        parts.append("## Evidence anchors")
        for ref in refs:
            parts.append(f"- {ref}")

    parts.append("")
    parts.append(
        "Goal: produce a concrete plan to ship the blocking step above. "
        "Identify the smallest viable sequence of subtasks, name files to "
        "touch, and surface any open decisions for the user."
    )

    return "\n".join(parts)
