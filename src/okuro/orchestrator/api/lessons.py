# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Lessons API — the review queue and the approve/reject decision, over the same functions the CLI drives.
# index:
#   imports
#   router
#   models
#   def _require_localhost_lessons
#   def list_lessons_endpoint
#   def approve_lesson_endpoint
#   def reject_lesson_endpoint
# AGENT_HEADER_END -->
"""Lessons API — a third front-end over ``okuro.sense.distill.lessons``.

- GET  /api/lessons?status=candidate&limit=20   the review queue
- POST /api/lessons/{id}/approve                body {approved_by}
- POST /api/lessons/{id}/reject                 body {reason, rejected_by?}

**No logic lives here.** The CLI (``okuro distill``), the MCP tools
(``distill_lesson_approve`` / ``_reject``) and these routes all call
:func:`~okuro.sense.distill.lessons.approve_lesson` and
:func:`~okuro.sense.distill.lessons.reject_lesson`. That is not tidiness — the
approval rule is enforced in the SCHEMA (migration 143 makes an active lesson
without ``approved_by`` AND ``approved_at`` unrepresentable), and a route that
reimplemented any part of it would be a fourth opinion about a decision the
database has already settled. Approving here cannot activate a lesson any more
than approving on the CLI can: it records the decision, and the next
maintenance pass promotes it only if corroboration and the dwell clock agree.

Refusals surface as 400 with the backing function's own message, so the reason
a decision did not land is the same sentence in every front-end.

Mutations are loopback-only, matching keyring/cortex/services/people/reminders.
A lesson becomes a rule okuro applies to itself, so the decision belongs to
whoever is at the machine.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.lessons")

router = APIRouter(prefix="/api/lessons", tags=["lessons"])

# The three queues distill_lessons supports. Validated here so a typo returns
# an empty list with a 400 rather than an empty list that looks like "nothing
# to review".
_STATUSES = ("candidate", "active", "retired")


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_lessons(request: Request) -> None:
    """Loopback guard — same pattern as reminders/keyring/services."""
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)


# ── Models ───────────────────────────────────────────────────────────


class ApproveRequest(BaseModel):
    approved_by: str


class RejectRequest(BaseModel):
    # Deliberately NOT constrained here. The rule that a reason must be
    # non-empty lives in reject_lesson(), which every front-end goes through;
    # a min_length on this model would be a second copy of it that could drift,
    # and it would answer with a 422 shaped nothing like the CLI's message.
    reason: str
    rejected_by: str = ""


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("")
def list_lessons_endpoint(
    status: str = Query(default="candidate", description="candidate|active|retired"),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """One queue of mined lessons, best-corroborated first.

    Evidence per lesson is bounded by ``lessons_for_review`` itself (three
    sessions, truncated snippets, read from ``distill_facets`` and never from
    ``agent_events``) — an unbounded review surface would rebuild the
    transcripts the distill pipeline exists to delete.
    """
    if status not in _STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(_STATUSES)}")

    from okuro.sense.distill.lessons import lessons_for_review

    return {"status": status, "lessons": lessons_for_review(status=status, limit=limit)}


@router.post("/{lesson_id}/approve")
def approve_lesson_endpoint(
    lesson_id: int, payload: ApproveRequest, request: Request
) -> dict:
    """Record a human approval. The second key, not an override."""
    _require_localhost_lessons(request)

    from okuro.sense.distill.lessons import approve_lesson

    try:
        return approve_lesson(lesson_id, approved_by=payload.approved_by)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/{lesson_id}/reject")
def reject_lesson_endpoint(
    lesson_id: int, payload: RejectRequest, request: Request
) -> dict:
    """Retire a lesson with the reason on the row.

    Ungated by design and cheap on purpose: removing a candidate rule is the
    SAFE direction, and gating it would keep a bad proposal in front of the owner
    until somebody with a key was available.
    """
    _require_localhost_lessons(request)

    from okuro.sense.distill.lessons import reject_lesson

    try:
        return reject_lesson(
            lesson_id, reason=payload.reason, rejected_by=payload.rejected_by
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
