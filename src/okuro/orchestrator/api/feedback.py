# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Flow feedback REST API (F3) — submit/fetch a finished flow's rating
#   and read the autopilot-decision learning join.
# index:
#   imports
#   router
#   class FeedbackSubmit
#   class FeedbackOut
#   def _load_finished_task
#   def submit_feedback_route
#   def get_feedback_route
#   def get_feedback_decisions_route
# AGENT_HEADER_END -->
"""Flow feedback API — HTTP surface for the F3 learning loop.

Endpoints
---------
POST /api/tasks/{task_id}/feedback              submit / re-rate a finished flow
GET  /api/tasks/{task_id}/feedback              fetch the rating (404 if unrated)
GET  /api/tasks/{task_id}/feedback/decisions    the autopilot-decision join

CLI-testable before the web UI ships — every endpoint works with curl/httpie.
Path resolution mirrors deliberation.py (~/.okuro/orchestrator/tasks).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from okuro.orchestrator import feedback as feedback_svc
from okuro.orchestrator.api.state_reader import _TERMINAL_TASK_STATUSES
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.orchestrator.api.feedback")

# Must match main.py / deliberation.py — the live tasks dir, not the src tree.
OKURO_ROOT = Path(os.environ.get("OKURO_ROOT", okuro_home() / "orchestrator"))
TASKS_DIR = OKURO_ROOT / "tasks"

router = APIRouter(prefix="/api/tasks", tags=["feedback"])


# ── Models ───────────────────────────────────────────────────────────


class FeedbackSubmit(BaseModel):
    usability: int = Field(..., ge=1, le=5, description="Usability score 1..5")
    outcome_class: str = Field(..., description="One of feedback.OUTCOME_CLASSES")
    comment: Optional[str] = Field(
        None, description="Forward note for the next flow to consider"
    )

    @field_validator("outcome_class")
    @classmethod
    def _valid_outcome(cls, v: str) -> str:
        if v not in feedback_svc.OUTCOME_CLASSES:
            raise ValueError(
                f"outcome_class must be one of {list(feedback_svc.OUTCOME_CLASSES)}"
            )
        return v


class FeedbackOut(BaseModel):
    task_id: str
    usability: int
    outcome_class: str
    comment: Optional[str] = None
    resolved_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────


def _load_finished_task(task_id: str):
    """Load a task, 404 if missing, 409 if it is not in a terminal state.

    Rating an in-flight flow is meaningless — the outcome isn't known yet — so
    the write path rejects any task whose status is not terminal.
    """
    from okuro.orchestrator.state import load_task

    task_path = TASKS_DIR / task_id
    if not task_path.exists():
        raise HTTPException(404, f"Task not found: {task_id}")
    task = load_task(task_id, TASKS_DIR)
    if task.status not in _TERMINAL_TASK_STATUSES:
        raise HTTPException(
            409,
            f"Task {task_id} is not finished (status={task.status!r}); "
            f"cannot rate a flow that is still running.",
        )
    return task


# ── Routes ───────────────────────────────────────────────────────────


@router.post("/{task_id}/feedback", response_model=FeedbackOut)
def submit_feedback_route(task_id: str, body: FeedbackSubmit) -> FeedbackOut:
    """Submit (or re-submit) the rating for a finished flow."""
    _load_finished_task(task_id)
    try:
        row = feedback_svc.record_feedback(
            task_id=task_id,
            usability=body.usability,
            outcome_class=body.outcome_class,
            comment=body.comment,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return FeedbackOut(**row)


@router.get("/{task_id}/feedback", response_model=FeedbackOut)
def get_feedback_route(task_id: str) -> FeedbackOut:
    """Fetch a flow's rating. 404 if it was never rated."""
    row = feedback_svc.get_feedback(task_id)
    if not row:
        raise HTTPException(404, f"No feedback recorded for task {task_id}")
    return FeedbackOut(**row)


@router.get("/{task_id}/feedback/decisions")
def get_feedback_decisions_route(task_id: str) -> dict[str, Any]:
    """The learning join: this flow's autopilot decisions labelled by outcome."""
    return feedback_svc.decisions_for_task(task_id, TASKS_DIR)
