# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Inbox REST API — thin read-only wrapper over okuro.sense.inbox (Phase 1).
# index: imports | router | def list_inbox_route | def reduce_inbox_route | def surface_inbox_route
# AGENT_HEADER_END -->
"""Inbox REST API — HTTP surface for the unified inbox overlay (Phase 1).

Thin wrapper over :mod:`okuro.sense.inbox`. All ranking/projection logic
lives in the sense layer; this module only adapts it to HTTP.

Endpoints
---------
GET    /api/inbox            ranked list (default: state='surfaced', limit=50)
POST   /api/inbox/reduce     manual reduce trigger (ops / verification)
POST   /api/inbox/surface    manual surfacing-gate trigger (ops / verification)

Auth: inherits the global ``BearerAuthMiddleware`` — no per-router auth.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from okuro.sense import inbox as inbox_svc

logger = logging.getLogger("okuro.orchestrator.api.inbox")

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


class DisposeBody(BaseModel):
    action: str
    snooze_until: Optional[str] = None


@router.get("")
def list_inbox_route(
    state: str = "surfaced",
    kind: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> dict:
    rows = inbox_svc.inbox_list(state=state, kind=kind, project=project, limit=limit)
    # Impression telemetry — the only signal that separates "he saw it and
    # ignored it" from "he never opened the inbox". Silent on failure.
    inbox_svc.log_impression(rows, state=state, kind=kind)
    return {"inbox": rows}


@router.post("/reduce")
def reduce_inbox_route() -> dict:
    """Run one inbox-reduce pass now and return its summary.

    Manual trigger for ops / verification — the daemon runs this every 30m
    and on startup. Read/projection only; preserves user disposition.
    """
    return inbox_svc.reduce_once()


@router.post("/surface")
def surface_inbox_route() -> dict:
    """Run one surfacing-gate pass now and return its summary.

    Manual trigger for ops / verification — the daemon ``inbox-heartbeat``
    runs this every 30m and on startup. Promotes high-value 'new' rows to
    'surfaced'; silent-when-empty, defer-when-busy.
    """
    return inbox_svc.surface_pass()


@router.get("/detail")
def inbox_detail_route(ref_table: str, ref_id: str) -> dict:
    """Resolve an inbox row's source body → markdown for inline expansion.

    Works for any ref_table (todos / thoughts / commitments / signals /
    reminder_suggestions); unknown tables return ``{"body": null}``.
    """
    return inbox_svc.inbox_detail(ref_table, ref_id)


@router.post("/{item_id}/dispose")
def dispose_inbox_route(item_id: str, body: DisposeBody) -> dict:
    """Apply a user disposition (act / defer / dismiss) to one inbox row.

    400 on an invalid action; 404 if no row matches ``item_id``.
    """
    try:
        row = inbox_svc.inbox_dispose(
            item_id, body.action, snooze_until=body.snooze_until
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not row:
        raise HTTPException(status_code=404, detail=f"inbox item {item_id} not found")
    return row
