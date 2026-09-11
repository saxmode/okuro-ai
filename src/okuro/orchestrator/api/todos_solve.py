# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: POST /api/todos/{id}/solve — mint inline session or kick orchestrator.
# index:
#   imports
#   router
#   models
#   def _claim_todo
#   def _release_claim
#   def solve_route
# AGENT_HEADER_END -->
"""Solve route — opens the chosen resolution path for a todo.

Two modes:

* ``mode='session'`` — calls :meth:`StreamRegistry.create` with
  ``return_token=True`` to mint a session-scoped bearer + sessions_inline
  row. Returns ``{session_id, bearer, inline_url}`` so the browser can
  open ``/inline/{session_id}`` and pass the bearer via ``postMessage``
  (avoids leaking the token into history via ``?t=``).

* ``mode='orchestrator'`` — spawns the orchestrator engine with
  ``--source-todo-id``. Returns ``{orchestrator_id, status_url}``.

Concurrency: a todo can only have one active resolution at a time.
``todos.claimed_session_id`` and ``todos.claimed_orchestrator_id``
(migration 042) are the lease — only one is set, both cleared when the
resolution terminates. The route refuses to mint a second resolution
on top of an existing claim (409).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.todos_solve")

router = APIRouter(prefix="/api/todos", tags=["todos"])


# ── Models ───────────────────────────────────────────────────────────


class SolveRequest(BaseModel):
    mode: Literal["session", "orchestrator"] = "session"
    provider: str = Field(default="claude")
    model: Optional[str] = None
    intelligence: Optional[str] = None


# ── Internal helpers ─────────────────────────────────────────────────


def _claim_todo(todo_id: str, *, session_id: Optional[str], orchestrator_id: Optional[str]) -> None:
    """Atomically mark a todo as claimed by one resolution.

    Sets exactly one of ``claimed_session_id`` / ``claimed_orchestrator_id``.
    Raises HTTPException(409) when either is already set.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT claimed_session_id, claimed_orchestrator_id, status "
        "FROM todos WHERE id = ?",
        (todo_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")
    if row["status"] in ("done", "dropped"):
        raise HTTPException(
            status_code=409,
            detail=f"todo is {row['status']}, refusing to resolve a closed todo",
        )
    if row["claimed_session_id"]:
        sess = db.fetchone(
            "SELECT status FROM sessions_inline WHERE id = ?",
            (row["claimed_session_id"],),
        )
        if sess is None or sess["status"] in ("done", "cancelled", "error"):
            db.execute(
                "UPDATE todos SET claimed_session_id = NULL WHERE id = ?",
                (todo_id,),
            )
        else:
            raise HTTPException(
                status_code=409,
                detail=f"todo already has an active session ({row['claimed_session_id']})",
            )
    if row["claimed_orchestrator_id"]:
        raise HTTPException(
            status_code=409,
            detail=f"todo already has an active orchestrator ({row['claimed_orchestrator_id']})",
        )

    db.execute(
        """
        UPDATE todos
           SET claimed_session_id = ?,
               claimed_orchestrator_id = ?,
               status = 'doing',
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (session_id, orchestrator_id, todo_id),
    )


def _release_claim(todo_id: str) -> None:
    """Clear the claim columns. Called on session/orchestrator spawn failure."""
    from okuro.db import get_db

    db = get_db()
    db.execute(
        """
        UPDATE todos
           SET claimed_session_id = NULL,
               claimed_orchestrator_id = NULL,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (todo_id,),
    )


# ── Endpoint ─────────────────────────────────────────────────────────


@router.post("/{todo_id}/solve")
def solve_route(todo_id: str, body: SolveRequest) -> dict:
    """Open a resolution path for the todo.

    Returns shape depends on mode:
      - session       → {mode, session_id, bearer, inline_url}
      - orchestrator  → {mode, orchestrator_id, status_url}
    """
    from okuro.sense import todos as todos_svc

    try:
        todo = todos_svc.todo_get(todo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")

    if body.mode == "session":
        return _open_session(todo_id, todo, body)
    return _open_orchestrator(todo_id, todo, body)


def _open_session(todo_id: str, todo: dict, body: SolveRequest) -> dict:
    """Mint a streaming session and return the bearer + inline URL."""
    from okuro.bridge.streaming import (
        AdapterNotImplemented,
        get_registry,
    )

    # Pre-flight the claim so we don't leak a session if the todo is busy.
    # The real claim row update happens after registry.create() succeeds
    # so we have the session id to record.
    from okuro.db import get_db

    db = get_db()
    pre = db.fetchone(
        "SELECT claimed_session_id, claimed_orchestrator_id, status FROM todos WHERE id = ?",
        (todo_id,),
    )
    if pre is None:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")
    if pre["claimed_session_id"]:
        sess = db.fetchone(
            "SELECT status FROM sessions_inline WHERE id = ?",
            (pre["claimed_session_id"],),
        )
        if sess is None or sess["status"] in ("done", "cancelled", "error"):
            db.execute(
                "UPDATE todos SET claimed_session_id = NULL WHERE id = ?",
                (todo_id,),
            )
        else:
            raise HTTPException(
                status_code=409,
                detail="todo already has an active resolution",
            )
    if pre["claimed_orchestrator_id"]:
        raise HTTPException(
            status_code=409,
            detail="todo already has an active resolution",
        )
    if pre["status"] in ("done", "dropped"):
        raise HTTPException(
            status_code=409,
            detail=f"todo is {pre['status']}, refusing to resolve a closed todo",
        )

    initial_prompt = todo["title"]
    if todo.get("detail"):
        initial_prompt = f"{todo['title']}\n\n{todo['detail']}"

    try:
        result = get_registry().create(
            provider=body.provider,
            model=body.model,
            messages=[{"role": "user", "content": initial_prompt}],
            todo_id=todo_id,
            return_token=True,
        )
    except AdapterNotImplemented as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("solve_route: registry.create failed for todo %s", todo_id)
        raise HTTPException(status_code=500, detail=f"session_create_failed: {exc}")

    if not isinstance(result, tuple) or len(result) != 2:
        # Defensive: should never happen with return_token=True.
        raise HTTPException(status_code=500, detail="registry did not return token")
    session_id, bearer = result

    try:
        _claim_todo(todo_id, session_id=session_id, orchestrator_id=None)
    except HTTPException:
        # Race: another caller claimed between pre-flight and now. Cancel
        # the session we just spawned so we don't strand an LLM child.
        try:
            get_registry().cancel(session_id)
        except Exception:  # noqa: BLE001
            logger.warning("solve_route: lost-race cleanup cancel failed for %s", session_id)
        raise

    return {
        "mode": "session",
        "session_id": session_id,
        "bearer": bearer,
        "inline_url": f"/inline/{session_id}",
        "provider": body.provider,
    }


def _open_orchestrator(todo_id: str, todo: dict, body: SolveRequest) -> dict:
    """Spawn the orchestrator engine with the todo as the task description."""
    # Lazy-import the engine spawn helper from main.py — avoids a cyclic
    # import at module load (main.py imports this router on startup).
    from okuro.orchestrator.api.main import (
        TASKS_DIR,
        spawn_orchestrator,
    )

    description = todo["title"]
    if todo.get("detail"):
        description = f"{todo['title']}\n\n{todo['detail']}"

    task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{todo_id[:8]}"

    cmd = [
        sys.executable,
        "-m",
        "okuro.orchestrator.engine",
        "--task-id",
        task_id,
        "--source-todo-id",
        todo_id,
    ]
    if body.intelligence:
        cmd.extend(["--intelligence", body.intelligence])
    if body.provider:
        cmd.extend(["--preferred-cli", body.provider])
    cmd.append(description)

    try:
        _claim_todo(todo_id, session_id=None, orchestrator_id=task_id)
    except HTTPException:
        raise

    try:
        pid = spawn_orchestrator(cmd, task_id)
    except Exception as exc:  # noqa: BLE001
        _release_claim(todo_id)
        logger.exception("solve_route: spawn_orchestrator failed for todo %s", todo_id)
        raise HTTPException(status_code=500, detail=f"orchestrator_spawn_failed: {exc}")

    if not pid:
        _release_claim(todo_id)
        raise HTTPException(status_code=500, detail="orchestrator_spawn_failed")

    # Ensure the task directory exists so /work/{id} renders something.
    (TASKS_DIR / task_id).mkdir(parents=True, exist_ok=True)

    return {
        "mode": "orchestrator",
        "orchestrator_id": task_id,
        "status_url": f"/work/{task_id}",
        "pid": pid,
    }
