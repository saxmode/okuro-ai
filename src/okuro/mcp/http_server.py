# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: HTTP MCP server — hosted inside okuro-daemon (or standalone).
# index:
#   imports
#   config helpers
#   auth middleware
#   session contextvar middleware
#   def build_app
#   async def serve
#   async def main
# AGENT_HEADER_END -->
"""HTTP MCP server (Streamable HTTP transport).

One okuro process serves every local client (Claude, Codex, Cursor, Gemini)
over loopback HTTP. Since subagent #15 folded ``okuro-mcpd`` into
``okuro-daemon``, the production host is the daemon — it spawns
:func:`serve` as an asyncio subtask alongside the cron scheduler. A
standalone ``python -m okuro.mcp.http_server`` entry is still available
for smoke tests and backfill, but the shipping topology is one process.

Wiring:
  * Shared tool registry lives in :mod:`okuro.mcp._registry`.
  * ``StreamableHTTPSessionManager`` from the MCP SDK owns session
    tracking, resumability hooks, and the JSON-RPC framing. It exposes a
    single ASGI endpoint.
  * Auth: ``Authorization: Bearer <token>`` — token is the value stored
    in the okuro keyring under ``mcp.http_token`` (auto-created on first
    boot with ``secrets.token_urlsafe(32)``).
  * Session id: the SDK surfaces ``Mcp-Session-Id`` as a response header
    on ``initialize`` and requires it on every follow-up. Our middleware
    copies whatever is on the incoming request into the
    :data:`okuro.sense.session_state.current_session_id` contextvar so
    compliance state stays scoped to the client session.
  * Bind: ``127.0.0.1`` only. LAN-only is a system principle.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
import yaml
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from okuro.sense.session_state import current_session_id

from ._registry import build_mcp_server
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.mcp.http")

CONFIG_PATH = okuro_home() / "config.yaml"
TOKEN_PATH = okuro_home() / "mcp_http_token"
DEFAULT_HOST = "127.0.0.1"
PORT_RANGE = range(3090, 3100)  # reserved for okuro services per conventions
MCP_MOUNT = "/mcp"


# ---------------------------------------------------------------------------
# Config + token helpers
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False))


def _pick_free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((DEFAULT_HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in {PORT_RANGE}")


def configured_port() -> int | None:
    """The configured port, or None. Never allocates, never writes.

    :func:`resolve_port` is for the process that will LISTEN: it picks a free
    port and persists it. Everyone else only wants to know where the daemon is,
    and calling resolve_port() to find out has a side effect they never asked
    for — on a machine that has not booted the daemon yet it allocates a port
    and writes ~/.okuro/config.yaml. That bit mcp_config._http_url(), which
    generates config blobs pointing AT the daemon: a scoped
    ``write_claude_config(agent_home, transport="http")`` mutated the real
    home's config. Callers that merely reference the daemon use this instead
    and treat None as "not booted yet".
    """
    cfg = _read_config()
    port = ((cfg.get("mcp") or {}).get("http") or {}).get("port")
    return int(port) if port else None


def resolve_port() -> int:
    """Return the port to listen on — persists the choice on first boot.

    Allocates and WRITES when unset. Only the listening process should call
    this; see :func:`configured_port` for a read-only lookup.
    """
    cfg = _read_config()
    mcp_cfg = cfg.get("mcp", {}) or {}
    http_cfg = mcp_cfg.get("http", {}) or {}
    port = http_cfg.get("port")
    if port:
        return int(port)

    port = _pick_free_port()
    cfg.setdefault("mcp", {}).setdefault("http", {})["port"] = port
    _write_config(cfg)
    log.info("allocated mcp.http.port=%d", port)
    return port


def get_or_create_token() -> str:
    """Return the bearer token — creates it on first boot.

    The token lives at ``~/.okuro/mcp_http_token`` (mode 0600). The encrypted
    keyring would be ideal, but it requires an unlock password at startup,
    which blocks the daemon from coming up under systemd without user
    interaction. A local loopback token that authenticates CLI tools
    against the daemon is already a low-value secret (an attacker with
    filesystem access has much worse options than reading this file).
    """
    if TOKEN_PATH.is_file():
        return TOKEN_PATH.read_text().strip()
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token + "\n")
    TOKEN_PATH.chmod(0o600)
    log.info("created new mcp http token at %s", TOKEN_PATH)
    return token


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject everything except /health without a valid bearer token."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer"}, status_code=401)
        provided = header.split(" ", 1)[1].strip()
        if not secrets.compare_digest(provided, self._token):
            return JSONResponse({"error": "bad token"}, status_code=401)
        return await call_next(request)


class SessionContextMiddleware(BaseHTTPMiddleware):
    """Plumb ``Mcp-Session-Id`` into the session_state contextvar for this request."""

    async def dispatch(self, request: Request, call_next):
        sid = request.headers.get("mcp-session-id") or "http-anon"
        token = current_session_id.set(sid)
        try:
            return await call_next(request)
        finally:
            current_session_id.reset(token)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def live_session_count(session_manager: StreamableHTTPSessionManager) -> Optional[int]:
    """How many MCP transports are actually connected. None = can't tell.

    NOT len(all_session_states()): that dict is pre-seeded with a "stdio" entry
    and never evicted (drop_session exists but nothing calls it — the SDK
    exposes no close hook), so it reported 1 forever and could not distinguish
    "nobody is connected" from "one agent connected once and died". A restart
    decision made on that number is a guess wearing a number's clothes.

    The SDK's _server_instances is the real registry — added on session create,
    deleted on crash/terminate. It is private, so treat a shape change on
    upgrade as "unknown" (None) rather than crashing /health or, worse, quietly
    reading 0 and declaring it safe to restart on top of a live agent.
    """
    instances = getattr(session_manager, "_server_instances", None)
    if not isinstance(instances, dict):
        return None
    return len(instances)


def build_app(session_manager: StreamableHTTPSessionManager, token: str) -> Starlette:
    async def health(_request: Request) -> Response:
        from okuro.daemon.scheduler import running_handlers

        sessions = live_session_count(session_manager)
        try:
            handlers = running_handlers()
        except Exception:  # a diagnostic must never take /health down
            handlers = []
        payload: dict = {
            "ok": True,
            # Live MCP transports. null when the SDK's internals moved.
            "session_count": sessions,
            # Cron tasks mid-flight. cortex runs ~200s of every 300s, so this is
            # non-empty most of the time — that's the point.
            "running_handlers": handlers,
            # The precondition for an unattended restart: nobody mid-call and
            # nothing mid-run. Fails CLOSED when session_count is unknown —
            # "can't tell" must never read as "safe".
            "restart_safe": sessions == 0 and not handlers,
        }
        # Surface daemon-level supervisor state (scheduler, mcp-http,
        # ingress) and per-adapter ingress state so sysinfo_service_health
        # callers and the web UI can drill into a single endpoint.
        try:
            from okuro.daemon.supervisor import health_snapshot
            payload["supervisor"] = health_snapshot()
        except Exception:
            pass
        try:
            from okuro.ingress.supervisor import active_snapshot
            payload["ingress"] = active_snapshot()
        except Exception:
            pass
        # Daemon code-drift: null when in sync, else how far behind HEAD this
        # long-lived process is. Pull side of the G1 alarm — sysinfo_service_
        # health and the web UI read this; the code-drift-alarm task pushes the
        # same signal to the log.
        try:
            from okuro.system.code_version import detect_code_drift
            payload["code_drift"] = detect_code_drift()
        except Exception:
            payload["code_drift"] = None
        return JSONResponse(payload)

    async def mcp_handler(request: Request) -> Response:
        await session_manager.handle_request(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        )
        # handle_request writes the response directly; return an empty
        # response so Starlette's plumbing is happy.
        return Response(status_code=200)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with session_manager.run():
            yield

    middleware = [
        Middleware(BearerAuthMiddleware, token=token),
        Middleware(SessionContextMiddleware),
    ]

    return Starlette(
        debug=False,
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Mount(MCP_MOUNT, app=_asgi_for_manager(session_manager)),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )


def _asgi_for_manager(manager: StreamableHTTPSessionManager):
    """Wrap the session manager's handle_request as an ASGI app."""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    return app


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def serve(
    stop_event: asyncio.Event | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    install_signal_handlers: bool = False,
) -> None:
    """Run the HTTP MCP server until ``stop_event`` is set.

    Used by :mod:`okuro.daemon` to host the MCP surface as an asyncio
    subtask alongside the cron scheduler — both tasks share the same
    ``stop_event`` so SIGTERM to the daemon process gracefully shuts both
    down.

    Args:
        stop_event: External shutdown signal. When set, uvicorn is asked
            to exit. If None, the coroutine runs until externally
            cancelled or signals fire (see ``install_signal_handlers``).
        host: Bind address — always loopback in production.
        port: Listen port. Defaults to :func:`resolve_port` (reads or
            allocates via ``~/.okuro/config.yaml``).
        install_signal_handlers: Only set to True when running
            standalone (``python -m okuro.mcp.http_server``). The daemon
            owns its own signal handling, so leave this False when
            spawning :func:`serve` as a subtask.
    """
    if port is None:
        port = resolve_port()

    server = build_mcp_server()
    # session_idle_timeout is load-bearing, not a nicety: the SDK evicts a
    # session from _server_instances only on an explicit client DELETE or after
    # this idle window. With it unset (the SDK default of None), a client that
    # drops without DELETE — a crashed CLI, a network blip, a reconnect that
    # mints a new session id — leaves its old session pinned forever. That makes
    # live_session_count climb monotonically, so /health.restart_safe latches to
    # False and restart_guard refuses every restart on top of sessions that no
    # longer exist. 600s comfortably exceeds any normal
    # request/SSE gap; a genuinely-idle live session that gets reaped simply
    # re-initializes on its next call (recoverable, the same cost as a restart).
    manager = StreamableHTTPSessionManager(
        app=server, json_response=False, stateless=False,
        session_idle_timeout=600,
    )
    token = get_or_create_token()

    app = build_app(manager, token)
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(config)

    # Optional watcher task: when stop_event fires, flip
    # uvicorn.should_exit so .serve() returns gracefully.
    watcher: asyncio.Task | None = None
    if stop_event is not None:
        async def _watch_stop() -> None:
            await stop_event.wait()
            uvicorn_server.should_exit = True
        watcher = asyncio.create_task(_watch_stop(), name="mcp-http-stop-watch")

    if install_signal_handlers:
        loop = asyncio.get_running_loop()

        def _signal_shutdown() -> None:
            uvicorn_server.should_exit = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _signal_shutdown)
            except NotImplementedError:
                pass

    log.info("uvicorn running on http://%s:%d%s (mcp http)", host, port, MCP_MOUNT)
    try:
        await uvicorn_server.serve()
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass


# Back-compat alias — smoke_http_mcp.py and older callers import ``_serve``.
async def _serve(host: str, port: int) -> None:  # pragma: no cover - compat shim
    await serve(stop_event=None, host=host, port=port, install_signal_handlers=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = resolve_port()
    asyncio.run(serve(stop_event=None, host=DEFAULT_HOST, port=port, install_signal_handlers=True))


if __name__ == "__main__":
    main()
