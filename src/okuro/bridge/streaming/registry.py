# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: In-memory streaming-session registry; the bridge_stream_* MCP tools live behind it.
# index:
#   imports
#   exceptions
#   class SessionEventBus
#   class StreamRegistry
#   def get_registry / reset_registry_for_tests
# AGENT_HEADER_END -->
"""Streaming-session registry.

Holds a process-local map of ``session_id`` → adapter so the four
``bridge_stream_*`` MCP tools can drive the same adapter across calls.
The DB row in ``sessions_inline`` is the durable record; the registry
is the runtime handle.

Providers without an adapter raise :class:`AdapterNotImplemented` — the
MCP tool surface converts that to a structured error so the web layer can
degrade gracefully without sprinkling provider checks. Today that covers
everything outside :data:`StreamRegistry.SUPPORTED_PROVIDERS`
(claude, codex, antigravity).

Wave 3c adds :class:`SessionEventBus` — a fan-out queue per session that
the SSE HTTP route subscribes to. The registry hooks the adapter's
``on_event`` AND the global ``ApprovalGate``'s listener so every event
(unified protocol from §2.1, including ``tool_approval_required`` from
the gate) lands on the bus with a per-session monotonic ``seq``. The
SSE consumer can rewind via the on-disk transcript (already
seq-stamped) and resume live streaming from the bus.

Concurrency: registry methods are synchronous wrappers around async
adapter calls. Spawn / send_input / cancel coroutines are scheduled on
the dedicated background event loop owned by the registry — this keeps
the MCP dispatch path (which may be sync) decoupled from the adapter's
asyncio internals, and avoids the trap of awaiting a coroutine inside an
event loop that's already pumping the MCP server.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets as _secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors — caught at the MCP tool boundary and translated to JSON payloads.
# ---------------------------------------------------------------------------


class AdapterNotImplemented(RuntimeError):
    """Raised when a non-claude provider is requested in wave 2.

    Carries the wave number where the adapter is scheduled (3) so the
    MCP-tool layer can surface a structured ``{"error":
    "adapter_not_implemented", "wave": 3}`` payload without hard-coding
    the constant.
    """

    def __init__(self, provider: str, wave: int = 3) -> None:
        super().__init__(
            f"streaming adapter for provider={provider!r} not implemented "
            f"(scheduled for wave {wave})"
        )
        self.provider = provider
        self.wave = wave


class SessionNotFound(LookupError):
    """No session with that id in the registry's in-memory map."""


class SessionAlreadyTerminal(RuntimeError):
    """Attempted to drive (send_input) a session that is done/cancelled/error."""


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _okuro_home() -> Path:
    """Return the okuro home dir, honouring $OKURO_HOME (matches embed/config)."""
    home_env = os.environ.get("OKURO_HOME")
    if home_env:
        return Path(home_env)
    return okuro_home()


def _sessions_root() -> Path:
    """Root for inline-session transcripts. Created on demand."""
    root = _okuro_home() / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _transcript_relative_path(session_id: str) -> str:
    """Stored on ``sessions_inline.transcript_path`` (relative — see contract §5).

    Wave 2 ships a flat layout — ``sessions/<session_id>.jsonl``. Wave 4
    is expected to partition by date as per contract §5; the column is
    stored relative so a layout migration changes the writer only.
    """
    return f"sessions/{session_id}.jsonl"


def _transcript_absolute(session_id: str) -> Path:
    return _okuro_home() / _transcript_relative_path(session_id)


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _resolve_inline_mcp_endpoint() -> tuple[str, int] | tuple[None, None]:
    """Best-effort lookup of the FastAPI port that hosts /mcp/v1.

    Reads ``OKURO_INLINE_MCP_URL`` / ``OKURO_API_URL`` env vars first
    (the deploy path), then falls back to the default orchestrator port.
    The function returns ``(host, port)``; callers compose the URL.
    """
    url_env = os.environ.get("OKURO_INLINE_MCP_URL") or os.environ.get(
        "OKURO_API_URL"
    )
    if url_env:
        # Light parse — protocol://host:port
        from urllib.parse import urlparse

        parsed = urlparse(url_env)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 13333)
        return host, port
    return "127.0.0.1", int(os.environ.get("OKURO_API_PORT", "13333"))


def _mint_session_token() -> tuple[str, str]:
    """Return (plaintext, sha256-hex)."""
    plaintext = _secrets.token_urlsafe(32)
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, digest


def _insert_session_row(
    *,
    session_id: str,
    todo_id: Optional[str],
    provider: str,
    model: Optional[str],
    transcript_relpath: str,
    mcp_token_hash: Optional[str] = None,
    mcp_token_expires_at: Optional[str] = None,
    is_agentic: bool = False,
    work_identity: Optional[dict] = None,
) -> None:
    """Write a sessions_inline row. ``todo_id`` is the parent todo, or
    NULL for an unparented streaming session (orchestrator subagent /
    bridge_stream_start, which open a session without a backing todo).

    sessions_inline.todo_id is nullable as of migration 060 — the NULL
    path stores NULL directly. We do NOT mint a placeholder todo; doing
    so flooded the inbox with `stream-stub-*` rows (the bug this fix
    eliminates at its root).

    ``work_identity`` binds the dispatch this session was spawned for to the
    row the per-session bearer token resolves to on every request. That is
    what makes the identity readable by the shared daemon, whose own
    environment belongs to no subagent (migration 122).

    ``subtask_role`` / ``agent_provider`` / ``session_type`` join it in
    migration 128. ``agent_provider`` is NOT the ``provider`` column two lines
    up: that one names the stream adapter ("claude"), this one is the telemetry
    label ("orch-<role>"). ``subtask_role`` is load-bearing rather than
    descriptive — it is what arms the M5+ role hard gate, and the row is its
    only channel on the streaming path, which passes no environment at all.
    """
    from okuro.db import get_db

    wi = dict(work_identity or {})
    db = get_db()
    with db.write():
        db.execute(
            """
            INSERT INTO sessions_inline (
                id, todo_id, provider, model, status,
                started_at, last_event_at, transcript_path,
                mcp_token_hash, mcp_token_expires_at, is_agentic,
                task_id, subtask_id, dispatch_epoch, agent_pid, agent_host,
                subtask_role, agent_provider, session_type
            ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                todo_id,
                provider,
                model,
                _utc_iso(),
                _utc_iso(),
                transcript_relpath,
                mcp_token_hash,
                mcp_token_expires_at,
                1 if is_agentic else 0,
                wi.get("task_id"),
                wi.get("subtask_id"),
                wi.get("dispatch_epoch"),
                wi.get("agent_pid"),
                wi.get("agent_host"),
                wi.get("subtask_role"),
                wi.get("agent_provider"),
                wi.get("session_type"),
            ),
        )


def _identity_env(work_identity: Optional[dict]) -> dict:
    """The work-identity environment handed to the spawned CLI.

    Derived from the same dict that is bound to the session row so the two
    channels cannot drift — the drift is precisely what made the env channel
    look sufficient while the daemon that serves the calls had nothing.
    """
    wi = dict(work_identity or {})
    if not wi.get("task_id"):
        return {}
    env = {"OKURO_TASK_ID": str(wi["task_id"])}
    if wi.get("subtask_id"):
        env["OKURO_SUBTASK_ID"] = str(wi["subtask_id"])
    if wi.get("dispatch_epoch"):
        env["OKURO_DISPATCH_EPOCH"] = str(wi["dispatch_epoch"])
    # Round 2 (128). The CLI's OWN process still benefits from these — its
    # hooks and any stdio MCP server it starts read them — and deriving them
    # from the same dict as the row keeps the two channels from drifting. The
    # ROW is what the shared daemon reads; this is the second channel, not the
    # authority. Before this, the streaming dispatcher set NONE of them.
    if wi.get("subtask_role"):
        env["OKURO_SUBTASK_ROLE"] = str(wi["subtask_role"])
    if wi.get("agent_provider"):
        env["OKURO_PROVIDER"] = str(wi["agent_provider"])
    if wi.get("session_type"):
        env["OKURO_SESSION_TYPE"] = str(wi["session_type"])
    return env


def _adapter_pid(adapter: Any) -> Optional[int]:
    """The spawned CLI's pid, or None when the adapter has no process
    (test fakes, spawn-per-turn adapters between turns)."""
    proc = getattr(adapter, "_process", None)
    pid = getattr(proc, "pid", None)
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def _bind_agent_process(session_id: str, adapter: Any, work_identity: dict) -> None:
    """Record the CLI pid on the session row, then read the binding back.

    The read-back is not belt-and-braces. Every mechanism this identity feeds
    — lease, reap, epoch fence — fails silently and OPEN when it is missing, so
    a broken binding produces no error anywhere and stays broken for months
    (measured: zero identified sessions, ever, before migration 122). Verifying
    at the one moment the expected value is still in hand turns a silent class
    of failure into a loud one.
    """
    import socket

    from okuro.sense.work_identity import WorkIdentity, verify_binding

    try:
        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        host = None
    pid = _adapter_pid(adapter)
    if pid or host:
        _update_session_row(session_id, agent_pid=pid, agent_host=host)
    verify_binding(
        session_id,
        WorkIdentity(
            task_id=str(work_identity.get("task_id") or ""),
            subtask_id=work_identity.get("subtask_id"),
        ),
    )


def _update_session_row(session_id: str, **fields: Any) -> None:
    """Patch the sessions_inline row. Silently no-ops when the row is gone
    (a registry that outlives its DB is benign — the runtime adapter is
    still the source of truth for in-memory state)."""
    if not fields:
        return
    from okuro.db import get_db

    db = get_db()
    cols = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [session_id]
    try:
        with db.write():
            db.execute(
                f"UPDATE sessions_inline SET {cols} WHERE id = ?",
                tuple(params),
            )
    except Exception:  # noqa: BLE001
        logger.exception("registry: sessions_inline update failed (session=%s)", session_id)


# ---------------------------------------------------------------------------
# SessionEventBus — fan-out queue used by the SSE HTTP route
# ---------------------------------------------------------------------------


class SessionEventBus:
    """Per-session fan-out queue for unified events.

    Each SSE subscriber owns a private ``asyncio.Queue`` and calls
    :meth:`subscribe` to register it for the duration of its stream.
    Publishers (adapter ``on_event``, ApprovalGate listener, the
    registry's own terminal-status emitter) call :meth:`publish` which
    pushes the event onto every subscriber's queue without awaiting.

    Why fan-out (queue per subscriber) and not a shared queue: SSE
    streams are independent — one slow consumer can't be allowed to
    starve another. We accept the bounded memory cost (max_queue per
    subscriber) and drop on overflow so a hung browser tab never wedges
    the producer side. Drop is logged once per subscriber per overflow
    period.

    The bus is owned by the registry's background loop. Subscribers
    that come from a different loop (the FastAPI request loop) get
    their events forwarded via ``loop.call_soon_threadsafe`` — that is
    what :meth:`publish` does when ``loop`` is supplied at subscribe
    time.
    """

    def __init__(self, *, max_queue: int = 1000) -> None:
        self._max_queue = max_queue
        # subscriber -> (queue, loop, drop_count)
        self._subs: list[tuple[asyncio.Queue, Optional[asyncio.AbstractEventLoop], list[int]]] = []
        self._lock = threading.Lock()

    def subscribe(
        self,
        queue: asyncio.Queue,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        with self._lock:
            self._subs.append((queue, loop, [0]))

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s[0] is not queue]

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict) -> None:
        """Push ``event`` onto every subscriber. Non-blocking."""
        with self._lock:
            subs = list(self._subs)
        for queue, loop, drop_count in subs:
            try:
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._safe_put, queue, event, drop_count)
                else:
                    self._safe_put(queue, event, drop_count)
            except RuntimeError:
                # Loop closed mid-publish — best-effort, log and continue.
                logger.debug("SessionEventBus: subscriber loop closed; skipping event")

    @staticmethod
    def _safe_put(queue: asyncio.Queue, event: dict, drop_count: list[int]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            drop_count[0] += 1
            if drop_count[0] in {1, 10, 100, 1000}:
                logger.warning(
                    "SessionEventBus: subscriber queue full, dropped %d events",
                    drop_count[0],
                )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class StreamRegistry:
    """Process-local map of adapters keyed by session_id.

    The registry owns one background asyncio event loop on a dedicated
    thread. The MCP tools (which may be invoked from sync code) call
    ``submit(coro)`` to schedule adapter operations on that loop and
    block on the resulting Future.
    """

    SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "codex", "antigravity")
    # Wave 3b — codex's spawn-per-turn adapter landed alongside claude.
    # Each adapter exports the same public protocol
    # (start / send_input / cancel / stream / poll_events) so the
    # registry stays provider-agnostic below this line.
    #
    # `gemini` was dropped on 2026-07-18 with the rest of the provider's
    # retirement — the CLI it drove stopped serving individual accounts on
    # 2026-06-18. `antigravity` (agy) took its place as the Google path the
    # same day; it is spawn-per-turn like codex, but emits prose rather than
    # a JSON event stream, so its turns carry no tool-call or usage events.

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}
        self._buses: dict[str, SessionEventBus] = {}
        # Per-session seq counter for gate-emitted events. The adapter
        # owns its own seq for native events; gate events flow through
        # an independent stamper because they originate off-thread and
        # need to coexist on the same monotonic line.
        self._seq_lock = threading.Lock()
        self._next_seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        # Gate-listener registration is one-shot per process; we install
        # it lazily on first session creation to keep the import graph
        # quiet for unit tests that never spin up a session.
        self._gate_listener_installed = False

    # -- loop plumbing -------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._loop_ready.clear()

            def _run() -> None:
                asyncio.set_event_loop(self._loop)
                assert self._loop is not None
                self._loop_ready.set()
                self._loop.run_forever()

            self._loop_thread = threading.Thread(
                target=_run, name="okuro-stream-registry", daemon=True
            )
            self._loop_thread.start()
            self._loop_ready.wait(timeout=5.0)
            return self._loop

    def _submit(self, coro: Any, timeout: Optional[float] = 10.0) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    # -- public API ----------------------------------------------------------

    def create(
        self,
        *,
        provider: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        tools: Optional[list[dict]] = None,
        todo_id: Optional[str] = None,
        adapter_factory: Optional[Any] = None,
        return_token: bool = False,
        is_agentic: bool = False,
        cwd: Optional[str] = None,
        extra_env: Optional[dict] = None,
        work_identity: Optional[dict] = None,
    ) -> Any:
        """Spawn a new streaming session.

        ``adapter_factory`` lets tests inject a fake adapter without
        patching imports — defaults to :class:`ClaudeStreamAdapter`.

        When ``return_token`` is False (default, MCP-tool behavior) the
        plaintext bearer is handed to the spawned CLI only — the DB
        stores the hash and the caller receives the session id.

        When ``return_token`` is True the method returns the tuple
        ``(session_id, bearer_plain)``. This path exists for the
        browser Solve flow (``/api/todos/{id}/solve``) which must
        return the bearer so the inline page can authenticate against
        ``/api/sessions/{id}/events``. The token is the same plaintext
        the CLI received — there is no second token.
        """
        if provider not in self.SUPPORTED_PROVIDERS:
            raise AdapterNotImplemented(provider, wave=3)

        session_id = uuid.uuid4().hex
        transcript_relpath = _transcript_relative_path(session_id)
        transcript_abs = _transcript_absolute(session_id)
        transcript_abs.parent.mkdir(parents=True, exist_ok=True)

        if adapter_factory is None:
            adapter_factory = self._default_adapter_factory(provider)

        # Mint a one-shot session-scoped bearer token. The plaintext is
        # only ever handed to the spawning CLI (via --mcp-config for
        # claude, via `codex mcp add` for the others); the DB
        # stores only the SHA-256 hash. The token expires when the
        # session terminates (cancel/done/error update_session_row clears
        # the hash). Wave 3a contract §4.2 Option A.
        token_plain, token_hash = _mint_session_token()
        host, port = _resolve_inline_mcp_endpoint()
        mcp_url = f"http://{host}:{port}/mcp/v1/" if host and port else None

        # ORCH-PARALLEL-TREE: forward the process cwd (worktree isolation)
        # ONLY when a caller actually set one. Keeping it additive — the
        # kwarg is omitted on the no-worktree default — preserves the
        # create() contract for adapter_factories (incl. test fakes /
        # third-party adapters) that don't accept ``cwd``. The real
        # claude/codex adapters all accept+spawn-with ``cwd``.
        _adapter_kwargs: dict[str, Any] = dict(
            session_id=session_id,
            model=model or self._default_model_for(provider),
            system_prompt=system,
            transcript_path=transcript_abs,
            mcp_endpoint_url=mcp_url,
            mcp_bearer_token=token_plain,
        )
        if cwd is not None:
            _adapter_kwargs["cwd"] = cwd
        # ROCK-SOLID v5 P4.1 — work identity for the spawned CLI, forwarded
        # exactly like ``cwd`` above and for the same reason: additive, so an
        # adapter_factory that does not accept ``env`` (test fakes, third-party
        # adapters) keeps working. The real claude/codex/antigravity adapters
        # all take env= and merge it into the subprocess environment.
        #
        # This is how OKURO_TASK_ID / OKURO_SUBTASK_ID / OKURO_DISPATCH_EPOCH
        # reach the subagent on the SESSION-LOOP path. The legacy
        # dispatcher.py path builds the same vars via _build_subagent_env;
        # this one had no env channel at all, so a subagent spawned by the
        # streaming dispatcher recorded no work identity and no dispatch
        # generation — which is most subagents.
        #
        # The env is now the SECOND channel, not the only one. It still serves
        # the subagent's own process (role telemetry, a stdio MCP server if one
        # is ever used again); the authoritative binding is on the session row
        # below, because the daemon that answers the subagent's MCP calls never
        # sees this environment.
        _env = dict(extra_env or {})
        _env.update(_identity_env(work_identity))
        if _env:
            _adapter_kwargs["env"] = _env
        adapter = adapter_factory(**_adapter_kwargs)

        # Wire the registry's lifecycle hook BEFORE spawn so the very
        # first event (status:running on system.init) bumps last_event_at.
        adapter.on_event(self._make_event_hook(session_id))

        # Create the per-session event bus + seed seq counter. Adapter
        # events arrive via on_event; gate events arrive via the global
        # gate listener installed below.
        bus = SessionEventBus()
        with self._lock:
            self._buses[session_id] = bus
        with self._seq_lock:
            self._next_seq[session_id] = 0
        adapter.on_event(self._make_bus_publisher(session_id))
        self._ensure_gate_listener()

        _insert_session_row(
            session_id=session_id,
            todo_id=todo_id,
            provider=provider,
            model=model,
            transcript_relpath=transcript_relpath,
            mcp_token_hash=token_hash,
            mcp_token_expires_at=None,
            is_agentic=is_agentic,
            work_identity=work_identity,
        )

        with self._lock:
            self._adapters[session_id] = adapter

        # Push the initial user messages (if any) after the process is up.
        # The brief allows messages=None for a fresh session; if messages
        # are passed they go in as user turns one after another.
        initial_messages = list(messages or [])

        async def _bootstrap() -> None:
            await adapter.start()
            for msg in initial_messages:
                # accept both {"role":"user","content":"..."} and a raw str
                if isinstance(msg, str):
                    text = msg
                elif isinstance(msg, dict):
                    text = str(msg.get("content") or msg.get("text") or "")
                else:
                    continue
                if not text:
                    continue
                await adapter.send_input(text)

        # Stream-start timeout. Claude is a true streaming adapter — start()
        # + send_input return quickly (the turn flows async), so 15s is ample.
        # Codex is SPAWN-PER-TURN (blocking): _bootstrap awaits the
        # whole first turn (slow startup + MCP handshake + the work itself,
        # ~45s+ observed), so a 15s cap times out → bridge_stream_start failed.
        # Give non-claude providers a generous start window.
        _start_timeout = 15.0 if provider == "claude" else 600.0
        self._submit(_bootstrap(), timeout=_start_timeout)

        # The spawned CLI's pid, known only here. The successor-engine reap
        # SIGTERMs the pid on the session row; on the HTTP transport
        # os.getpid() inside a tool call is the SHARED DAEMON, so a reap
        # keyed on that would signal okuro itself. Recorded after start()
        # because that is when the process exists.
        if work_identity:
            _bind_agent_process(session_id, adapter, work_identity)

        if return_token:
            return session_id, token_plain
        return session_id

    def get(self, session_id: str) -> Any:
        with self._lock:
            adapter = self._adapters.get(session_id)
        if adapter is None:
            raise SessionNotFound(session_id)
        return adapter

    def has(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._adapters

    def send_input(self, session_id: str, message: str) -> dict:
        adapter = self.get(session_id)
        if adapter.status in {"done", "cancelled", "error"}:
            raise SessionAlreadyTerminal(
                f"session {session_id!r} is terminal ({adapter.status})"
            )
        # Spawn-per-turn adapters (codex) block here until the
        # turn completes — use a generous timeout to cover model latency.
        self._submit(adapter.send_input(message), timeout=600.0)
        # Persist the upstream CLI's session/thread id once the adapter
        # has captured it (codex assigns its own thread id).
        provider_session_id = getattr(adapter, "thread_id", None)
        if provider_session_id:
            _update_session_row(session_id, provider_session_id=provider_session_id)
        return {"ok": True}

    def cancel(self, session_id: str) -> dict:
        adapter = self.get(session_id)
        self._submit(adapter.cancel(), timeout=10.0)
        _update_session_row(
            session_id,
            status=adapter.status,
            ended_at=adapter.ended_at or _utc_iso(),
            last_event_at=adapter.last_event_at or _utc_iso(),
            # Invalidate the per-session bearer immediately so any
            # in-flight CLI tool call rejects with 401. Hash null-out
            # is the canonical "session token retired" signal.
            mcp_token_hash=None,
        )
        # Drop from in-memory map — the row stays as the audit record.
        # Keep the bus for a beat so any in-flight SSE consumer sees the
        # final status event before the stream closes; the SSE route
        # itself drops the bus reference when it exits.
        with self._lock:
            self._adapters.pop(session_id, None)
        return {"ok": True, "status": adapter.status}

    def events(self, session_id: str, since_seq: int = 0) -> dict:
        adapter = self.get(session_id)
        events = adapter.poll_events(since_seq=since_seq)
        next_seq = max((int(e.get("seq", -1)) for e in events), default=since_seq - 1) + 1
        done = adapter.status in {"done", "cancelled", "error"}
        return {"events": events, "next_seq": next_seq, "done": done}

    def list_active(self) -> list[str]:
        with self._lock:
            return list(self._adapters.keys())

    # -- SSE / event bus -----------------------------------------------------

    def get_bus(self, session_id: str) -> SessionEventBus:
        """Return the per-session event bus.

        Auto-creates an empty bus for sessions that exist on disk but
        not in the in-memory map (post-restart reconnect). Subscribers
        still get the transcript replay; live events arrive once the
        session is back in memory (a wave-3+ resume path).
        """
        with self._lock:
            bus = self._buses.get(session_id)
            if bus is None:
                bus = SessionEventBus()
                self._buses[session_id] = bus
            return bus

    def replay_events(
        self, session_id: str, *, since_seq: int = 0
    ) -> list[dict]:
        """Read the on-disk transcript and return unified events with seq >= since_seq.

        Used by the SSE route on connect to catch the browser up before
        switching to the live bus. Returns events in seq order. Unknown
        types (raw stderr envelopes etc.) are filtered out — only the
        protocol vocabulary from :data:`UNIFIED_EVENT_TYPES` makes it
        out.
        """
        from .claude import UNIFIED_EVENT_TYPES

        path = _transcript_absolute(session_id)
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            if evt.get("type") not in UNIFIED_EVENT_TYPES:
                continue
            try:
                seq = int(evt.get("seq", -1))
            except (TypeError, ValueError):
                continue
            if seq >= since_seq:
                out.append(evt)
        out.sort(key=lambda e: int(e.get("seq", 0)))
        return out

    def status(self, session_id: str) -> dict:
        """Return a JSON-safe status snapshot for the /status route.

        Reads from the DB row first (the durable record) and falls back
        to the in-memory adapter only when the row is missing. Token
        counts come from the most-recent ``usage`` event written to the
        transcript — we don't aggregate (a future wave can).
        """
        from okuro.db import get_db

        db = get_db()
        try:
            row = db.fetchone(
                """
                SELECT id, todo_id, provider, model, status,
                       started_at, last_event_at, ended_at, transcript_path
                FROM sessions_inline WHERE id = ?
                """,
                (session_id,),
            )
        except Exception:  # noqa: BLE001
            row = None
        if not row:
            adapter = self._adapters.get(session_id)
            if adapter is None:
                raise SessionNotFound(session_id)
            row = {
                "id": session_id,
                "provider": getattr(adapter, "provider", None),
                "model": getattr(adapter, "model", None),
                "status": adapter.status,
                "started_at": adapter.started_at,
                "last_event_at": adapter.last_event_at,
                "ended_at": adapter.ended_at,
            }

        # Walk the transcript backwards for the latest usage event. We
        # cap the scan at 1 MB — beyond that an SSE consumer would have
        # had to be live long enough to receive the events naturally.
        usage = self._latest_usage(session_id)
        # Pull pending approvals from the gate.
        try:
            from okuro.sense.approval_gate import get_gate

            pending = get_gate().pending_for_session(session_id)
        except Exception:  # noqa: BLE001
            pending = []
        out = dict(row)
        out["usage"] = usage
        out["pending_approvals"] = pending
        with self._seq_lock:
            out["next_seq"] = self._next_seq.get(session_id, 0)
        return out

    def _latest_usage(self, session_id: str) -> dict:
        path = _transcript_absolute(session_id)
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict) and evt.get("type") == "usage":
                return {
                    "input_tokens": evt.get("input_tokens", 0),
                    "output_tokens": evt.get("output_tokens", 0),
                    "cost_usd": evt.get("cost_usd", 0),
                }
        return {}

    # -- internals ----------------------------------------------------------

    def _default_adapter_factory(self, provider: str) -> Any:
        """Return the adapter class bound to a provider string.

        Lazy-imported per call so the registry doesn't pay the import
        cost (and side-effects) for adapters it never instantiates.
        """
        if provider == "claude":
            from .claude import ClaudeStreamAdapter

            return ClaudeStreamAdapter
        if provider == "codex":
            from .codex import CodexStreamAdapter

            return CodexStreamAdapter
        if provider == "antigravity":
            from .antigravity import AntigravityStreamAdapter

            return AntigravityStreamAdapter
        raise AdapterNotImplemented(provider, wave=3)

    @staticmethod
    def _default_model_for(provider: str) -> str:
        """Per-provider default model when the caller doesn't pin one.

        Picked to match each CLI's "well-known sensible default" — claude
        sonnet, codex gpt-5-codex, agy the mid Gemini tier. Overridden by
        the explicit ``model`` argument to ``create()``.

        agy model names are DISPLAY strings (spaces + parens) as listed by
        `agy models` — not api ids.
        """
        return {
            "claude": "sonnet",
            "codex": "gpt-5-codex",
            "antigravity": "Gemini 3.5 Flash (High)",
        }.get(provider, "sonnet")

    def _make_bus_publisher(self, session_id: str) -> Any:
        """Forward adapter events onto the per-session bus.

        Adapter events already carry a ``seq`` stamped by the adapter
        itself — we keep that seq for them. The wider per-session seq
        counter the registry tracks is only consulted for events that
        originate OUTSIDE the adapter (e.g. gate listener emissions).

        We still bump :data:`_next_seq` so gate events that interleave
        with adapter events stay strictly monotonic across the session.
        """

        def _hook(payload: dict) -> None:
            try:
                seq = int(payload.get("seq", 0))
            except (TypeError, ValueError):
                seq = 0
            with self._seq_lock:
                current = self._next_seq.get(session_id, 0)
                if seq + 1 > current:
                    self._next_seq[session_id] = seq + 1
            bus = self._buses.get(session_id)
            if bus is not None:
                bus.publish(payload)

        return _hook

    def _ensure_gate_listener(self) -> None:
        """Install one process-wide gate listener that routes events.

        The gate emits ``tool_approval_required`` / ``tool_call_start`` /
        ``tool_call_done`` events carrying a ``session_id``. Each event
        is stamped with a registry-side seq, written to the transcript
        (so reconnects can replay them), and published on the bus.

        Idempotent — multiple sessions share one listener; the listener
        dispatches by ``session_id`` in the payload.
        """
        if self._gate_listener_installed:
            return
        try:
            from okuro.sense.approval_gate import get_gate
        except Exception:  # noqa: BLE001
            logger.debug("registry: approval_gate import unavailable")
            return

        def _listener(payload: dict) -> None:
            try:
                session_id = payload.get("session_id")
                if not session_id:
                    return
                # Only forward events that belong to a session we know
                # about — the gate is process-wide, the registry isn't.
                if session_id not in self._buses:
                    return
                event = self._stamp_gate_event(session_id, payload)
                self._persist_transcript_line(session_id, event)
                bus = self._buses.get(session_id)
                if bus is not None:
                    bus.publish(event)
            except Exception:  # noqa: BLE001
                logger.exception("registry: gate listener crashed")

        get_gate().add_event_listener(_listener)
        self._gate_listener_installed = True

    def _stamp_gate_event(self, session_id: str, payload: dict) -> dict:
        """Stamp a gate event with a session-monotonic seq.

        The gate's listener fires from whichever loop owns the HTTP
        request — we don't trust its seq numbering. Mint our own and
        keep the original gate fields verbatim.
        """
        with self._seq_lock:
            seq = self._next_seq.get(session_id, 0)
            self._next_seq[session_id] = seq + 1
        out = dict(payload)
        out["seq"] = seq
        out.setdefault("session_id", session_id)
        out.setdefault("ts", _utc_iso())
        return out

    def _persist_transcript_line(self, session_id: str, event: dict) -> None:
        """Append a gate event to the on-disk transcript.

        Adapter events are persisted by the adapter itself; gate events
        flow around the adapter so we record them here so reconnects
        and audit replay see the same stream the browser saw.
        """
        path = _transcript_absolute(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event, default=str) + "\n")
        except OSError:
            logger.exception("registry: transcript append failed (session=%s)", session_id)

    def _make_event_hook(self, session_id: str) -> Any:
        """Build a synchronous callback the adapter fires on every event.

        Wave 2 keeps the work cheap (a single UPDATE on
        ``sessions_inline.last_event_at``). Heavier downstream work
        (web SSE pubsub, approval-gate hooks) lands in wave 3 / 4.
        """

        def _hook(payload: dict) -> None:
            try:
                fields: dict[str, Any] = {"last_event_at": payload.get("ts") or _utc_iso()}
                if payload.get("type") == "status":
                    state = payload.get("state")
                    if state in {"running", "paused", "done", "cancelled", "error"}:
                        fields["status"] = state
                        if state in {"done", "cancelled", "error"}:
                            fields["ended_at"] = payload.get("ts") or _utc_iso()
                            # Note: we keep ``mcp_token_hash`` set on a
                            # terminal status transition so the browser
                            # can read the close-out transcript over
                            # the SSE endpoint with the same bearer.
                            # The HTTP MCP middleware enforces
                            # status='running' independently (strict
                            # mode in verify_session_bearer) so a CLI
                            # tool call on a terminal session still
                            # rejects 401. ``cancel()`` explicitly nulls
                            # the hash for the SIGTERM/SIGKILL path.
                _update_session_row(session_id, **fields)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "registry: event hook crashed (session=%s)", session_id
                )

        return _hook


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_registry: Optional[StreamRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> StreamRegistry:
    """Return the process-local registry, creating it on first call."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = StreamRegistry()
    return _registry


def reset_registry_for_tests() -> None:
    """Drop the singleton — tests use this to start from a clean map."""
    global _registry
    with _registry_lock:
        _registry = None


__all__ = [
    "AdapterNotImplemented",
    "SessionAlreadyTerminal",
    "SessionEventBus",
    "SessionNotFound",
    "StreamRegistry",
    "get_registry",
    "reset_registry_for_tests",
]
