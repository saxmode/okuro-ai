# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Todos REST API — thin wrapper over okuro.sense.todos.
# index:
#   imports
#   router
#   models
#   def list_todos_route
#   def create_todo_route
#   def get_todo_route
#   def update_todo_route
#   def complete_todo_route
#   def delete_todo_route
# AGENT_HEADER_END -->
"""Todos REST API — HTTP surface for the dedicated `todos` table.

Mirrors the MCP tool set so the web UI (and ad-hoc curl) can reach the
same actionable-item registry agents write to via todo_add.

Endpoints
---------
GET    /api/todos                   list (default: open+doing)
POST   /api/todos                   create
GET    /api/todos/{id}              single
PATCH  /api/todos/{id}              partial update
POST   /api/todos/{id}/complete     shorthand → status='done'
DELETE /api/todos/{id}              hard-delete (status='dropped' is soft)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from okuro.sense import todos as todos_svc

logger = logging.getLogger("okuro.orchestrator.api.todos")

router = APIRouter(prefix="/api/todos", tags=["todos"])


# ── Models ───────────────────────────────────────────────────────────


class TodoCreate(BaseModel):
    title: str
    detail: Optional[str] = None
    priority: int = Field(default=3, ge=1, le=5)
    project: Optional[str] = None
    due_at: Optional[str] = None
    source: str = "user"
    source_event_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    reminder_id: Optional[str] = None


class TodoPatch(BaseModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=1, le=5)
    project: Optional[str] = None
    due_at: Optional[str] = None
    reminder_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("")
def list_todos_route(
    status: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> dict:
    try:
        # verbose=True: the HTTP client has no token ceiling, and the UI
        # renders fields the compact view drops. Keeps this payload as it was.
        rows = todos_svc.todo_list(
            status=status, project=project, limit=limit, verbose=True
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"todos": rows}


@router.post("")
def create_todo_route(body: TodoCreate) -> dict:
    try:
        return todos_svc.todo_add(**body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{todo_id}")
def get_todo_route(todo_id: str) -> dict:
    try:
        return todos_svc.todo_get(todo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")


@router.patch("/{todo_id}")
def update_todo_route(todo_id: str, body: TodoPatch) -> dict:
    try:
        return todos_svc.todo_update(
            todo_id, **body.model_dump(exclude_none=True)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{todo_id}/complete")
def complete_todo_route(todo_id: str) -> dict:
    try:
        return todos_svc.todo_done(todo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")


@router.delete("/{todo_id}")
def delete_todo_route(todo_id: str) -> dict:
    try:
        return todos_svc.todo_delete(todo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")
