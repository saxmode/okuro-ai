# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: HTTP+SSE endpoints for inline streaming sessions — events / input / approve / cancel / status.
# index:
#   imports
#   def _extract_token
#   class _SessionBearerMiddleware
#   async def session_events_sse
#   async def session_input
#   async def session_approve
#   async def session_cancel
#   async def session_status
#   def build_sessions_app / mount_sessions_routes
# AGENT_HEADER_END -->
"""HTTP + SSE surface for okuro inline streaming sessions (wave 3c).

Mounts under ``/api/sessions`` on the main okuro FastAPI app. Every
route is gated by the per-session bearer token minted at
``bridge_stream_start`` and stored as the SHA-256 hash in
``sessions_inline.mcp_token_hash`` — same surface as the inline MCP
mount at ``/mcp/v1``. The bearer middleware reuses
:func:`okuro.mcp.inline_http.verify_session_bearer` so there is one
canonical check.

Routes:

  GET  /api/sessions/{id}/events?since_seq=N    SSE stream
  POST /api/sessions/{id}/input                 {"message": "..."}
  POST /api/sessions/{id}/approve               {"invocation_id", "decision", "edited_args"?}
  POST /api/sessions/{id}/cancel                {}
  GET  /api/sessions/{id}/status                read-only metadata

The browser passes the per-session bearer either in the
``Authorization: Bearer <token>`` header (POST routes; fetch() can set
it) OR in the ``?t=<token>`` query param (EventSource can't set
headers — contract §3.1).

Path scoping: this surface is mounted UNDER /api/, but the global
``BearerAuthMiddleware`` in :mod:`okuro.orchestrator.api.main` is
configured to skip ``/api/sessions/`` so that the per-session check
below is the only auth on this path. The two checks would conflict
(global bearer vs. per-session bearer) without that exemption.

The session id in the path must match the session id resolved from the
bearer — a token leaked to another tab can't be used to control a
different session. This is enforced in :class:`_SessionBearerMiddleware`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("okuro.web.session_sse")

SESSIONS_PREFIX = "/api/sessions"

# Heartbeat cadence on the SSE channel — matches the contract's
# "every 15s" requirement.
_HEARTBEAT_SEC = 15.0
# How long to wait for a live event before checking for a heartbeat /
# terminal state. Short enough to keep the connection responsive,
# long enough to avoid busy-looping.
_LIVE_POLL_SEC = 0.5


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> Optional[str]:
    """Read the per-session bearer from header or ?t= query param.

    SSE consumers (EventSource) cannot attach a custom header, so the
    ``?t=<token>`` form is the contract — same hack the dashboard
    telemetry endpoints already use. POST routes are expected to use
    the header (fetch() can set it) but both are accepted for symmetry.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip() or None
    qs = request.query_params.get("t") or request.query_params.get("token")
    if qs:
        return qs
    return None


def _resolve_session(request: Request) -> Optional[dict[str, Any]]:
    from okuro.mcp.inline_http import verify_session_bearer

    token = _extract_token(request)
    if not token:
        return None
    # allow_terminal=True so the browser can still drain the final
    # transcript over SSE after the session has hit done/cancelled/
    # error. The MCP HTTP mount keeps strict mode — a tool call on a
    # terminal session is a bug, but reading the close-out is normal.
    return verify_session_bearer(token, allow_terminal=True)


class _SessionBearerMiddleware(BaseHTTPMiddleware):
    """Auth gate — token validity + session id path match.

    Returns 401 on missing/invalid token, 403 when the path session id
    differs from the bearer's session id (cross-session abuse).
    Successful requests stash the resolved row on ``request.state``.
    """

    async def dispatch(self, request: Request, call_next):
        # When the sub-app is mounted, request.url.path holds the FULL
        # request path (mount prefix included) while request.scope's
        # ``root_path`` carries the mount prefix. Strip the prefix so
        # we can extract the {session_id} segment robustly regardless
        # of how deeply the host app composes mounts.
        full_path = request.url.path
        root_path = request.scope.get("root_path") or ""
        relative = full_path[len(root_path):] if full_path.startswith(root_path) else full_path
        first = relative.lstrip("/").split("/", 1)[0]
        if not first:
            return JSONResponse({"error": "missing_session_id"}, status_code=404)

        row = _resolve_session(request)
        if row is None:
            return JSONResponse({"error": "invalid_session_token"}, status_code=401)
        if row.get("id") != first:
            return JSONResponse({"error": "session_id_mismatch"}, status_code=403)
        request.state.session_row = row
        return await call_next(request)


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


def _format_sse(event: dict) -> str:
    """Serialise one event as an SSE frame.

    Frame shape (contract §2.2 — single channel, JSON discriminator):

        event: <type>
        id: <seq>
        data: <json>
        \n
    """
    etype = str(event.get("type", "okuro"))
    seq = event.get("seq")
    out = [f"event: {etype}"]
    if isinstance(seq, int):
        out.append(f"id: {seq}")
    try:
        data = json.dumps(event, default=str)
    except (TypeError, ValueError):
        data = json.dumps({"type": "error", "code": "unencodable"})
    out.append(f"data: {data}")
    return "\n".join(out) + "\n\n"


_TERMINAL_STATES = frozenset({"done", "cancelled", "error"})


def _is_terminal(event: dict) -> bool:
    return (
        event.get("type") == "status"
        and event.get("state") in _TERMINAL_STATES
    )


async def session_events_sse(request: Request) -> Response:
    """Stream unified events to the browser over SSE.

    Replay phase: emit every transcript event with ``seq >= since_seq``
    so a browser reconnect resumes from where it left off (contract
    §2.2 / §3.1). Then switch to live mode, draining the per-session
    bus until the session reaches a terminal state OR the consumer
    disconnects.
    """
    session_id: str = request.path_params["session_id"]
    try:
        since_seq = int(request.query_params.get("since_seq", "0"))
    except (TypeError, ValueError):
        since_seq = 0

    from okuro.bridge.streaming import get_registry

    registry = get_registry()
    bus = registry.get_bus(session_id)

    # Subscribe BEFORE replay so any live event that arrives while we
    # are replaying the transcript is captured rather than lost.
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    loop = asyncio.get_event_loop()
    bus.subscribe(queue, loop=loop)

    async def _gen():
        try:
            # Heartbeat the SSE connection on open so proxies see bytes
            # before the first event lands (contract §3.1 — matches the
            # ``: connected`` comment style preview.py already uses).
            yield ": connected\n\n"

            seen_seqs: set[int] = set()
            terminal_seen = False

            # ---------------- replay phase ----------------
            replay = registry.replay_events(session_id, since_seq=since_seq)
            for evt in replay:
                seq_val = evt.get("seq")
                if isinstance(seq_val, int):
                    seen_seqs.add(seq_val)
                yield _format_sse(evt)
                if _is_terminal(evt):
                    terminal_seen = True

            if terminal_seen:
                yield "event: end\ndata: {}\n\n"
                return

            # ---------------- live phase ----------------
            heartbeat_at = loop.time() + _HEARTBEAT_SEC
            while not terminal_seen:
                # Detect client disconnect early so the producer side
                # doesn't keep filling our queue forever.
                if await request.is_disconnected():
                    return
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=_LIVE_POLL_SEC)
                except asyncio.TimeoutError:
                    now = loop.time()
                    if now >= heartbeat_at:
                        yield ": ping\n\n"
                        heartbeat_at = now + _HEARTBEAT_SEC
                    continue
                if not isinstance(evt, dict):
                    continue
                seq_val = evt.get("seq")
                # Suppress events the replay already covered.
                if isinstance(seq_val, int):
                    if seq_val in seen_seqs:
                        continue
                    seen_seqs.add(seq_val)
                yield _format_sse(evt)
                if _is_terminal(evt):
                    terminal_seen = True
                    yield "event: end\ndata: {}\n\n"
                    return
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # nginx / caddy disable buffering so heartbeats reach the
            # browser within the 15s SLA the contract assumes.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# POST /input
# ---------------------------------------------------------------------------


async def session_input(request: Request) -> Response:
    session_id: str = request.path_params["session_id"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    message = body.get("message")
    if not isinstance(message, str) or not message:
        return JSONResponse({"error": "missing_message"}, status_code=400)

    from okuro.bridge.streaming import (
        SessionAlreadyTerminal,
        SessionNotFound,
        get_registry,
    )

    registry = get_registry()
    try:
        # send_input runs the adapter coroutine on the registry's
        # background loop and blocks on its result. We offload to a
        # thread so the FastAPI loop stays responsive.
        await asyncio.to_thread(registry.send_input, session_id, message)
    except SessionNotFound:
        return JSONResponse({"error": "session_not_found"}, status_code=404)
    except SessionAlreadyTerminal as exc:
        return JSONResponse(
            {"error": "session_already_terminal", "detail": str(exc)},
            status_code=409,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_input: send failed for %s", session_id)
        return JSONResponse({"error": "send_failed", "detail": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# POST /approve
# ---------------------------------------------------------------------------


async def session_approve(request: Request) -> Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body"}, status_code=400)
    invocation_id = body.get("invocation_id")
    decision = body.get("decision")
    edited_args = body.get("edited_args")
    if not isinstance(invocation_id, str) or not invocation_id:
        return JSONResponse({"error": "missing_invocation_id"}, status_code=400)
    if decision not in {"approve", "deny", "edit"}:
        return JSONResponse(
            {"error": "bad_decision", "decision": decision}, status_code=400
        )
    if decision == "edit" and not isinstance(edited_args, dict):
        return JSONResponse({"error": "missing_edited_args"}, status_code=400)

    from okuro.sense.approval_gate import get_gate

    gate = get_gate()
    result = gate.resolve(
        invocation_id=invocation_id,
        decision=decision,
        edited_args=edited_args if decision == "edit" else None,
    )
    # gate returns {"ok": True, ...} or {"ok": False, "error": "..."}
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /cancel
# ---------------------------------------------------------------------------


async def session_cancel(request: Request) -> Response:
    session_id: str = request.path_params["session_id"]
    from okuro.bridge.streaming import SessionNotFound, get_registry

    registry = get_registry()
    try:
        result = await asyncio.to_thread(registry.cancel, session_id)
    except SessionNotFound:
        # Idempotent — cancelling a session that's already gone is OK.
        return JSONResponse({"ok": True, "status": "gone"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_cancel: failed for %s", session_id)
        return JSONResponse({"error": "cancel_failed", "detail": str(exc)}, status_code=500)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


async def session_status(request: Request) -> Response:
    session_id: str = request.path_params["session_id"]
    from okuro.bridge.streaming import SessionNotFound, get_registry

    registry = get_registry()
    try:
        snapshot = registry.status(session_id)
    except SessionNotFound:
        return JSONResponse({"error": "session_not_found"}, status_code=404)
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_status: failed for %s", session_id)
        return JSONResponse({"error": "status_failed", "detail": str(exc)}, status_code=500)
    return JSONResponse(snapshot)


# ---------------------------------------------------------------------------
# Sub-app factory
# ---------------------------------------------------------------------------


def build_sessions_app() -> Starlette:
    """Build the Starlette sub-app mounted at ``/api/sessions``.

    Routes use ``{session_id}`` path params; the bearer middleware
    pulls the same id from the token row and rejects mismatches.
    """
    routes = [
        Route(
            "/{session_id}/events",
            endpoint=session_events_sse,
            methods=["GET"],
        ),
        Route(
            "/{session_id}/input",
            endpoint=session_input,
            methods=["POST"],
        ),
        Route(
            "/{session_id}/approve",
            endpoint=session_approve,
            methods=["POST"],
        ),
        Route(
            "/{session_id}/cancel",
            endpoint=session_cancel,
            methods=["POST"],
        ),
        Route(
            "/{session_id}/status",
            endpoint=session_status,
            methods=["GET"],
        ),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(_SessionBearerMiddleware)],
    )


def mount_sessions_routes(app: Any) -> None:
    """Mount the sub-app on a FastAPI / Starlette host.

    Idempotent — repeated calls become no-ops once the route is bound.
    """
    for route in getattr(app, "routes", []):
        if getattr(route, "path", "") == SESSIONS_PREFIX:
            return
    sub = build_sessions_app()
    app.mount(SESSIONS_PREFIX, sub, name="inline-sessions")
    logger.info("mounted inline session routes at %s", SESSIONS_PREFIX)


__all__ = [
    "SESSIONS_PREFIX",
    "build_sessions_app",
    "mount_sessions_routes",
    "session_approve",
    "session_cancel",
    "session_events_sse",
    "session_input",
    "session_status",
]
