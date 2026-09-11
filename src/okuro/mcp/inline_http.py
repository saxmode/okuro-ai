# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Inline-session HTTP MCP mount — FastAPI sub-app under /mcp/v1.
# index:
#   imports
#   def verify_session_bearer
#   class InlineMcpBearerMiddleware
#   class InlineMcpSessionContextMiddleware
#   def build_inline_mcp_app
#   def inline_mcp_lifespan
#   def mount_inline_mcp
# AGENT_HEADER_END -->
"""HTTP MCP transport for inline web sessions (wave 3a).

This module is the FastAPI-mounted sibling of the standalone
``okuro.mcp.http_server`` daemon. Functionally identical at the MCP
protocol layer — both wrap :class:`StreamableHTTPSessionManager` — but
this one:

  * mounts under the okuro FastAPI app at the versioned path ``/mcp/v1``
    so a single uvicorn process serves the SPA, the orchestrator API,
    AND the per-session MCP target the spawned CLIs talk to.
  * uses a **per-session** bearer token (issued by
    ``bridge_stream_start`` and stored as a SHA-256 hash on
    ``sessions_inline.mcp_token_hash``) instead of the global
    ``~/.okuro/mcp_http_token``. Every request must carry an
    Authorization header whose token hashes to a row that is still
    ``status='running'`` and whose ``mcp_token_expires_at`` (when set)
    hasn't passed.
  * plumbs the resolved ``sessions_inline.id`` into the
    :data:`okuro.sense.approval_gate.current_inline_session_id`
    contextvar so the shared dispatcher's gate consult fires.

Path versioning: ``/mcp/v1`` is the wire contract. Future protocol bumps
go behind ``/mcp/v2``; both can coexist until clients migrate.

Bearer auth model: this mount is exempt from the FastAPI app's global
``BearerAuthMiddleware`` (configured in ``orchestrator.api.main``) — the
global middleware sees the path prefix and skips it. The check below is
the only gate on this surface, and it is per-session rather than
per-user.
"""

from __future__ import annotations

import hashlib
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("okuro.mcp.inline_http")

INLINE_MCP_PREFIX = "/mcp/v1"


def hash_token(token: str) -> str:
    """SHA-256 hex digest. The DB only ever stores the hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Bearer verifier — reads sessions_inline by token hash, validates expiry
# ---------------------------------------------------------------------------


def verify_session_bearer(
    token: str, *, allow_terminal: bool = False
) -> Optional[dict[str, Any]]:
    """Return the matching ``sessions_inline`` row dict or None.

    A row is valid when:
      * ``mcp_token_hash`` matches ``hash_token(token)``,
      * ``status`` is ``running`` (default) — set ``allow_terminal``
        to also accept ``paused`` / ``done`` / ``cancelled`` / ``error``
        for read-only reconnection paths (web SSE replay needs this so
        a browser can fetch the final events after the session ended).
      * ``mcp_token_expires_at`` is NULL OR strictly in the future.

    The MCP HTTP transport always uses the strict mode (``running``-only)
    because executing a tool call on a terminal session is a bug; the
    web SSE surface uses ``allow_terminal`` so the browser can still
    read the close-out transcript after the agent finishes.
    """
    if not token:
        return None
    try:
        from okuro.db import get_db

        db = get_db()
    except Exception:  # noqa: BLE001
        logger.debug("inline_http: db unavailable, denying")
        return None

    digest = hash_token(token)
    try:
        row = db.fetchone(
            """
            SELECT id, status, mcp_token_expires_at
              FROM sessions_inline
             WHERE mcp_token_hash = ?
             LIMIT 1
            """,
            (digest,),
        )
    except Exception:  # noqa: BLE001
        logger.exception("inline_http: token lookup failed")
        return None
    if not row:
        return None
    if not allow_terminal and row.get("status") != "running":
        return None
    exp = row.get("mcp_token_expires_at")
    if exp:
        # Lexicographic ISO-8601 comparison is monotonic; cheaper than parsing.
        if exp <= _utc_iso():
            return None
    return row


# ---------------------------------------------------------------------------
# Middleware — auth + contextvar plumbing
# ---------------------------------------------------------------------------


class InlineMcpBearerMiddleware(BaseHTTPMiddleware):
    """Per-session bearer check.

    Authorization is read from the standard ``Authorization: Bearer
    <token>`` header. Missing / invalid tokens return 401. A passing
    request stashes the resolved session id on ``request.state`` so the
    contextvar middleware (next in the chain) can promote it.
    """

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing_bearer"}, status_code=401)
        token = header.split(" ", 1)[1].strip()
        row = verify_session_bearer(token)
        if row is None:
            return JSONResponse({"error": "invalid_session_token"}, status_code=401)
        request.state.inline_session_id = row["id"]
        return await call_next(request)


class InlineMcpSessionContextMiddleware(BaseHTTPMiddleware):
    """Promote ``request.state.inline_session_id`` into the contextvars.

    The shared MCP dispatcher reads this contextvar to decide whether to
    consult the approval gate. Stdio + standalone HTTP MCP requests leave
    it None and the gate is skipped.

    THIS IS ALSO THE WORK-IDENTITY BOUNDARY. One daemon serves every
    subagent over HTTP, so its process environment answers "which dispatch
    is calling?" with the daemon's own (empty) answer — which is why the
    lease, the reap and the C12 epoch fence were all inert on the live
    deploy. The bearer token already resolved to the caller's session row;
    the identity bound to that row at spawn is therefore available here, per
    request, and every consumer reads it through
    ``okuro.sense.work_identity.resolve_work_identity``.
    """

    async def dispatch(self, request: Request, call_next):
        from okuro.sense.approval_gate import current_inline_session_id
        from okuro.sense.session_state import current_session_id
        from okuro.sense.work_identity import (
            bind_work_identity,
            identity_for_session,
        )

        inline_id = getattr(request.state, "inline_session_id", None)
        sess_token = current_session_id.set(inline_id or "inline-anon")
        inline_token = current_inline_session_id.set(inline_id)
        work_token = bind_work_identity(
            identity_for_session(inline_id) if inline_id else None
        )
        try:
            return await call_next(request)
        finally:
            from okuro.sense.work_identity import current_work_identity

            current_work_identity.reset(work_token)
            current_inline_session_id.reset(inline_token)
            current_session_id.reset(sess_token)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_inline_mcp_app() -> Starlette:
    """Build the Starlette sub-app to mount at ``/mcp/v1``.

    The app:
      * runs the StreamableHTTP session manager as its only route,
      * is wrapped by per-session bearer auth + contextvar plumbing,
      * uses Starlette lifespan to start / stop the session manager's
        own runtime (the SDK requires ``async with manager.run():``
        around any request dispatch).
    """
    # Importing the SDK + registry lazily so unit tests of the module
    # surface (token hashing, bearer verification) don't pay the SDK
    # import cost.
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from ._registry import build_mcp_server

    server = build_mcp_server()
    manager = StreamableHTTPSessionManager(
        app=server, json_response=False, stateless=False
    )
    global _inline_manager
    _inline_manager = manager

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "inline_mcp", "version": "v1"})

    async def mcp_handler(request: Request) -> Response:
        await manager.handle_request(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        )
        return Response(status_code=200)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    routes = [
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/", endpoint=mcp_handler, methods=["GET", "POST", "DELETE"]),
    ]
    middleware = [
        Middleware(InlineMcpBearerMiddleware),
        Middleware(InlineMcpSessionContextMiddleware),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


# The session manager built by build_inline_mcp_app, kept so /api/health can
# report how many agents are actually attached to /mcp/v1. Without it the
# orchestrator has no way to know whether a restart would 404 someone's live
# session — the same blind spot the daemon had. One inline app per process, so
# a module global matches reality.
_inline_manager = None


def live_session_count() -> int | None:
    """Agents currently attached to /mcp/v1. None = cannot tell.

    Mirrors okuro.mcp.http_server.live_session_count — same SDK, same private
    registry, same rule: a shape change on upgrade must read as "unknown", never
    as "nobody home". Returns 0 (not None) when the inline app was never built,
    because a process with no inline MCP genuinely has no sessions to lose.
    """
    if _inline_manager is None:
        return 0
    instances = getattr(_inline_manager, "_server_instances", None)
    if not isinstance(instances, dict):
        return None
    return len(instances)


_inline_sub_app: Starlette | None = None


def mount_inline_mcp(app: Any) -> None:
    """Mount the inline-MCP sub-app on a FastAPI / Starlette app.

    Idempotent — repeated calls become no-ops once the route is registered.

    NOTE: Starlette/FastAPI does NOT propagate sub-app lifespans to the
    parent. The parent app MUST call ``inline_mcp_lifespan()`` from its
    own lifespan, otherwise the StreamableHTTPSessionManager's task
    group never starts and every /mcp/v1 request 500s with
    "Task group is not initialized".
    """
    global _inline_sub_app
    for route in getattr(app, "routes", []):
        if getattr(route, "path", "") == INLINE_MCP_PREFIX:
            return
    _inline_sub_app = build_inline_mcp_app()
    app.mount(INLINE_MCP_PREFIX, _inline_sub_app, name="inline-mcp")
    logger.info("mounted inline MCP at %s", INLINE_MCP_PREFIX)


@asynccontextmanager
async def inline_mcp_lifespan():
    """Enter the inline-MCP sub-app's own lifespan from the parent.

    Yields after the sub-app's lifespan startup completes; on exit
    runs the sub-app shutdown. No-op if the sub-app hasn't been
    mounted yet.
    """
    if _inline_sub_app is None:
        yield
        return
    async with _inline_sub_app.router.lifespan_context(_inline_sub_app):
        yield


__all__ = [
    "INLINE_MCP_PREFIX",
    "build_inline_mcp_app",
    "hash_token",
    "mount_inline_mcp",
    "verify_session_bearer",
]
