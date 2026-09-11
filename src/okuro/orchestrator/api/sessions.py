# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Inline-session lifecycle endpoints — bearer reissue (wave 5).
# index:
#   imports
#   router
#   models
#   def reissue_session_bearer
# AGENT_HEADER_END -->
"""Inline-session lifecycle endpoints (wave 5).

The `/api/sessions/{id}/{events,input,approve,cancel,status}` surface is
gated by the per-session bearer minted at ``bridge_stream_start``. When
the browser reloads it loses that bearer (we never persist it — too
sensitive), so the user is locked out of a still-running session even
though the agent process is alive. This module mints a fresh
per-session bearer using the **global** API token, so any browser the
user is signed into on this machine can reattach to a running session.

The endpoint lives at ``POST /api/sessions/{id}/reissue-bearer`` and is
auth'd by the global ``BearerAuthMiddleware`` — NOT the per-session
bearer middleware mounted under ``/api/sessions``. The bearer-prefix
exemption is overridden for this specific suffix in main.py so the
global check applies.

Restrictions
------------
* Only sessions in ``running`` or ``paused`` status can be reissued —
  reissuing a terminal session is a no-op and would mint a token that
  can't drive anything.
* Wave 5 only covers session-claimed todos. Orchestrator-claimed todos
  use a different surface (no per-session bearer to reissue).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ── Models ───────────────────────────────────────────────────────────


class ReissueBearerResponse(BaseModel):
    """Response payload for POST /api/sessions/{id}/reissue-bearer."""

    bearer: str
    inline_url: str


# ── Endpoint ─────────────────────────────────────────────────────────


_REISSUABLE_STATES = ("running", "paused")


@router.post("/{session_id}/reissue-bearer", response_model=ReissueBearerResponse)
def reissue_session_bearer(session_id: str) -> ReissueBearerResponse:
    """Mint a fresh per-session bearer for an in-flight inline session.

    Used by the browser when the page reloads and the in-memory bearer
    is gone but the underlying session (and its sessions_inline row) is
    still alive. The new token replaces ``sessions_inline.mcp_token_hash``
    so the old plaintext (held by the spawned CLI subprocess) is also
    invalidated. The expires_at column is updated to NULL — wave 5 keeps
    the no-expiry policy.

    Auth: global API bearer (the caller has lost the per-session one).

    Returns
    -------
    bearer : str
        The plaintext token to use as ``Authorization: Bearer <bearer>``
        against ``/api/sessions/{id}/*`` and ``/mcp/v1`` routes.
    inline_url : str
        Convenience path for the inline-session frontend route.

    Raises
    ------
    404 — session id not in sessions_inline.
    409 — session is in a terminal state (done/cancelled/error).
    """
    from okuro.bridge.streaming.registry import _mint_session_token
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT id, status FROM sessions_inline WHERE id = ?",
        (session_id,),
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"session {session_id} not found"
        )
    status = row.get("status")
    if status not in _REISSUABLE_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session is {status!r}, cannot reissue bearer "
                f"(must be one of {list(_REISSUABLE_STATES)})"
            ),
        )

    plaintext, digest = _mint_session_token()
    try:
        with db.write():
            db.execute(
                """
                UPDATE sessions_inline
                   SET mcp_token_hash = ?,
                       mcp_token_expires_at = NULL
                 WHERE id = ?
                """,
                (digest, session_id),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "reissue_session_bearer: DB update failed for session %s", session_id
        )
        raise HTTPException(
            status_code=500, detail=f"reissue_failed: {exc}"
        )

    return ReissueBearerResponse(
        bearer=plaintext, inline_url=f"/inline/{session_id}"
    )


__all__ = ["router", "reissue_session_bearer", "ReissueBearerResponse"]
