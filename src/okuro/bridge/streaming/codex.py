# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: CodexStreamAdapter — spawn-per-turn codex CLI subprocess + unified-event translator.
# index:
#   imports
#   def translate_codex_event
#   def inline_mcp_server_name
#   def inline_bearer_env_name
#   class CodexStreamAdapter
# AGENT_HEADER_END -->
"""Codex CLI streaming adapter for inline web sessions.

Codex shares gemini's spawn-per-turn semantics (contract §1.1, §7-B) but
differs in two important ways from the gemini adapter:

  1. Codex assigns its own thread_id on the first turn (emitted as
     ``thread.started.thread_id`` in the JSONL stream). Subsequent turns
     must use ``codex exec resume <thread_id> --json``. The adapter
     captures that id from the stream and exposes it via the
     :attr:`thread_id` property; the registry persists it to
     ``sessions_inline.provider_session_id`` so a backend restart can
     resume the same conversation.

  2. The inline okuro MCP server is injected PER-SPAWN via ``-c`` config
     overrides (not a persistent ``codex mcp add`` registration, which
     leaks one entry per session into the global ``~/.codex/config.toml``
     forever). Codex only completes the streamable-HTTP handshake when
     ``experimental_use_rmcp_client=true`` is set, so that flag ships on
     every spawn alongside the server table:
     ``-c experimental_use_rmcp_client=true``,
     ``-c mcp_servers.<key>.url="…/mcp/v1/"``,
     ``-c mcp_servers.<key>.bearer_token_env_var="OKURO_INLINE_BEARER_<short>"``.
     The bearer itself is env-var-indirect: codex reads it from that env
     var at spawn time; the adapter sets it in ``_build_env``. The server
     key is session-unique so it never collides with the user's own
     ``[mcp_servers.okuro]`` stdio entry in the global config.

Sandbox decision (contract Q5, recommendation A): inline sessions
always run with ``--sandbox workspace-write``. The okuro tier matrix
inside the MCP middleware is the primary gate for okuro tool calls;
the codex sandbox is the backstop for the CLI's built-in shell
tooling. ``--skip-git-repo-check`` is set so inline sessions work
outside a git workspace.

Per-turn invocation (verified against codex-cli 0.130.0):

  First turn:
    ``codex exec --json --sandbox workspace-write --skip-git-repo-check
        --dangerously-bypass-approvals-and-sandbox -m <model> "<msg>"``

  Subsequent turns (thread_id assigned by first turn):
    ``codex exec resume <thread_id> --json --sandbox workspace-write
        --skip-git-repo-check
        --dangerously-bypass-approvals-and-sandbox -m <model> "<msg>"``

The ``--dangerously-bypass-approvals-and-sandbox`` is intentional: we
gate at okuro's MCP layer, not codex's. Contract §1.4.

Stderr discipline matches gemini/claude: tee to transcript, never to
the SSE stream.

Item-level events only: ``thread.started`` / ``turn.started`` /
``item.completed`` / ``turn.completed`` — no token deltas (contract Q3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Translator (pure function — easy to unit-test in isolation)
# ---------------------------------------------------------------------------


def translate_codex_event(raw: dict, *, seq: int, session_id: str, ts: str) -> list[dict]:
    """Translate one codex `exec --json` line into unified events.

    Pure function: no I/O, no clock reads. Mirrors the claude / gemini
    translator structure.

    Codex event shapes (contract §1.2):

      ``{"type":"thread.started", "thread_id":"…"}``
      ``{"type":"turn.started"}``
      ``{"type":"item.completed", "item":{"id":"…", "type":"agent_message", "text":"…"}}``
      ``{"type":"item.completed", "item":{"id":"…", "type":"function_call", "name":"…", "arguments":"…"}}``
      ``{"type":"item.completed", "item":{"id":"…", "type":"function_call_output", "output":"…", "call_id":"…"}}``
      ``{"type":"item.completed", "item":{"id":"…", "type":"mcp_tool_call", "server":"…", "tool":"…", "arguments":{…}, "result":{"content":[…]}, "status":"completed"}}``
      ``{"type":"turn.completed", "usage":{"input_tokens":…, "output_tokens":…}}``
      ``{"type":"error", "error":"…"}`` (defensive)

    Unknown shapes → no-op; raw line still hits the transcript.
    """
    if not isinstance(raw, dict):
        return []

    kind = raw.get("type")
    out: list[dict] = []

    def _envelope(payload: dict) -> dict:
        evt: dict[str, Any] = {
            "type": payload["type"],
            "session_id": session_id,
            "seq": seq + len(out),
            "ts": ts,
        }
        for key, value in payload.items():
            if key == "type":
                continue
            evt[key] = value
        return evt

    # --- thread.started -----------------------------------------------
    if kind == "thread.started":
        out.append(
            _envelope(
                {
                    "type": "status",
                    "state": "running",
                    "detail": {
                        "subtype": "thread_started",
                        "codex_thread_id": raw.get("thread_id"),
                    },
                }
            )
        )
        return out

    # --- turn.started -------------------------------------------------
    if kind == "turn.started":
        # No unified equivalent — turn boundaries are inferred from
        # the surrounding user_echo + assistant message_stop pair. We
        # could emit a 'status:running' here, but the noise outweighs
        # the value. Stays a no-op.
        return out

    # --- item.completed -----------------------------------------------
    if kind == "item.completed":
        item = raw.get("item") or {}
        if not isinstance(item, dict):
            return out
        itype = item.get("type")
        item_id = item.get("id")

        if itype == "agent_message":
            text = item.get("text") or item.get("content") or ""
            out.append(
                _envelope(
                    {
                        "type": "message_start",
                        "message_id": item_id,
                        "role": "assistant",
                    }
                )
            )
            out.append(
                _envelope(
                    {
                        "type": "message_stop",
                        "message_id": item_id,
                        "text": text if isinstance(text, str) else json.dumps(text),
                        "stop_reason": item.get("stop_reason"),
                    }
                )
            )
            return out

        if itype == "function_call":
            args_raw = item.get("arguments")
            # codex serialises function arguments as a JSON string; parse
            # so the unified event surfaces a structured object.
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except (TypeError, ValueError):
                    args = {"_raw": args_raw}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}
            out.append(
                _envelope(
                    {
                        "type": "tool_call_start",
                        "call_id": item.get("call_id") or item_id,
                        "tool_name": item.get("name"),
                        "args": args,
                        "tier": "read",
                        "mcp_server": item.get("mcp_server"),
                    }
                )
            )
            return out

        if itype == "function_call_output":
            output = item.get("output")
            is_error = bool(item.get("is_error") or item.get("error"))
            out.append(
                _envelope(
                    {
                        "type": "tool_call_done",
                        "call_id": item.get("call_id") or item_id,
                        "outcome": "error" if is_error else "auto",
                        "result": output,
                        "error": (output if is_error else None),
                        "duration_ms": item.get("duration_ms"),
                    }
                )
            )
            return out

        if itype == "mcp_tool_call":
            # Codex 0.142 reports MCP tool calls as a SINGLE item that
            # transitions item.started(in_progress) → item.completed. The
            # fields differ from function_call/function_call_output: `tool`
            # (not `name`), `server` (not `mcp_server`), `arguments` already a
            # dict, and a `result` MCP envelope {"content":[{type,text}]}.
            # We emit a matched tool_call_start + tool_call_done pair off the
            # single completed item (call_id = the stable item id) so the
            # streaming dispatcher can pair them and trigger the reviewer on
            # artifact_write. Without this the whole codex MCP tool surface is
            # invisible to the engine: no review, no FE feed, await_review
            # polls forever. item.started for mcp_tool_call stays a no-op to
            # avoid a duplicate start.
            args_raw = item.get("arguments")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except (TypeError, ValueError):
                    args = {"_raw": args_raw}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}
            call_id = item.get("call_id") or item_id
            status = item.get("status")
            err = item.get("error")
            is_error = bool(err) or status in {"failed", "error"}
            # Normalise the MCP result envelope to the TextContent-list shape
            # _extract_artifact_id (and claude's tool_call_done) already speak.
            raw_result = item.get("result")
            if isinstance(raw_result, dict) and isinstance(
                raw_result.get("content"), list
            ):
                result_payload: Any = raw_result["content"]
            else:
                result_payload = raw_result
            out.append(
                _envelope(
                    {
                        "type": "tool_call_start",
                        "call_id": call_id,
                        "tool_name": item.get("tool") or item.get("name"),
                        "args": args,
                        "tier": "read",
                        "mcp_server": item.get("server") or item.get("mcp_server"),
                    }
                )
            )
            out.append(
                _envelope(
                    {
                        "type": "tool_call_done",
                        "call_id": call_id,
                        "outcome": "error" if is_error else "auto",
                        "result": result_payload,
                        "error": (err or (result_payload if is_error else None)),
                        "duration_ms": item.get("duration_ms"),
                    }
                )
            )
            return out

        # Unknown item type — no-op (caller still archives the raw line).
        return out

    # --- turn.completed -----------------------------------------------
    if kind == "turn.completed":
        usage = raw.get("usage") or {}
        if usage:
            out.append(
                _envelope(
                    {
                        "type": "usage",
                        # turn.completed IS the terminal accounting for codex —
                        # same role claude's `result` plays, so it carries the
                        # same marker and the dispatcher treats both alike.
                        "final": True,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        # Present on codex builds that report caching; absent
                        # ones read 0, which is honest rather than missing.
                        "cache_read_input_tokens": usage.get(
                            "cached_input_tokens",
                            usage.get("cache_read_input_tokens", 0),
                        ),
                        "cache_creation_input_tokens": usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                        "cost_usd": 0,  # subscription — contract §9
                    }
                )
            )
        return out

    # --- error (defensive) --------------------------------------------
    if kind == "error":
        out.append(
            _envelope(
                {
                    "type": "error",
                    "code": raw.get("code", "codex_error"),
                    "message": raw.get("error") or raw.get("message") or "",
                    "recoverable": False,
                }
            )
        )
        return out

    return out


# ---------------------------------------------------------------------------
# MCP attachment helpers — best-effort wrappers around the codex CLI
# ---------------------------------------------------------------------------


def _short_session(session_id: str) -> str:
    return session_id.replace("-", "")[:12]


def inline_mcp_server_name(session_id: str) -> str:
    """Session-unique MCP server key injected per-spawn via ``-c``.

    Used as the ``mcp_servers.<key>`` table name in the codex config
    override. Session-scoped so it never collides with the user's own
    ``[mcp_servers.okuro]`` entry in the global ``~/.codex/config.toml``.
    """
    return f"okuro-inline-{_short_session(session_id)}"


def inline_bearer_env_name(session_id: str) -> str:
    """Env-var name codex reads at spawn time for the bearer token.

    codex mcp add's ``--bearer-token-env-var`` plumbs an env var name
    rather than the bearer itself; the adapter sets the env var on
    every spawn (see :meth:`CodexStreamAdapter._build_env`).
    """
    return f"OKURO_INLINE_BEARER_{_short_session(session_id).upper()}"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _StreamState:
    """Mutable runtime state for one codex adapter.

    Duplicated from claude / gemini by design — three small dataclasses
    is clearer than one shared with optional fields.
    """

    seq: int = 0
    status: str = "running"
    last_event_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    thread_id: Optional[str] = None  # codex-assigned; populated on first turn
    turn_count: int = 0


class CodexStreamAdapter:
    """Spawn-per-turn codex CLI subprocess + unified-event translator.

    Lifecycle:
      * ``start()`` — primes transcript, attaches per-session MCP entry.
      * ``send_input(msg)`` — first turn spawns ``codex exec --json …``;
        subsequent turns spawn ``codex exec resume <thread_id> --json …``.
        The thread_id is captured from the first ``thread.started`` event.
      * ``cancel()`` — SIGTERM/SIGKILL any in-flight subprocess, detach
        MCP entry, push terminal sentinel.

    Public protocol matches claude + gemini.
    """

    DEFAULT_BINARY = "codex"
    DEFAULT_SANDBOX = "workspace-write"  # contract Q5 recommendation A

    def __init__(
        self,
        *,
        session_id: str,
        model: str = "gpt-5-codex",
        system_prompt: Optional[str] = None,
        transcript_path: Path,
        mcp_endpoint_url: Optional[str] = None,
        mcp_bearer_token: Optional[str] = None,
        binary: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        subprocess_factory: Optional[Any] = None,
        mcp_runner: Any = subprocess.run,
        terminate_grace_sec: float = 2.0,
        sandbox: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.system_prompt = system_prompt
        self.transcript_path = transcript_path
        self.mcp_endpoint_url = mcp_endpoint_url
        self.mcp_bearer_token = mcp_bearer_token
        self.binary = binary or self.DEFAULT_BINARY
        self.cwd = cwd
        self._extra_env = dict(env or {})
        self._subprocess_factory = subprocess_factory or asyncio.create_subprocess_exec
        self._mcp_runner = mcp_runner
        self._terminate_grace_sec = terminate_grace_sec
        self.sandbox = sandbox or self.DEFAULT_SANDBOX

        self._state = _StreamState()
        if thread_id:
            # Allows the registry to rehydrate an existing session — first
            # turn skips the create-new path and goes straight to resume.
            self._state.thread_id = thread_id
        self._process: Any = None
        self._event_queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        self._readers: list[asyncio.Task] = []
        self._transcript_lock = asyncio.Lock()
        self._on_event_callbacks: list[Any] = []
        self._closed = False
        self._mcp_attached = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._state.started_at is not None:
            raise RuntimeError(
                f"CodexStreamAdapter for session {self.session_id!r} already started"
            )

        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.transcript_path.exists():
            self.transcript_path.touch(mode=0o600)

        self._state.started_at = _iso_now()
        self._state.last_event_at = self._state.started_at

        # The inline MCP server is injected per-spawn via `-c` in
        # _build_cmd (no global `codex mcp add` registration), so there is
        # nothing to attach here — just record whether it will be wired.
        self._mcp_attached = bool(self.mcp_endpoint_url and self.mcp_bearer_token)

        await self._emit(
            {
                "type": "status",
                "state": "running",
                "detail": {
                    "model": self.model,
                    "provider": "codex",
                    "sandbox": self.sandbox,
                    "mcp_attached": self._mcp_attached,
                    "codex_thread_id": self._state.thread_id,
                },
            }
        )

    async def send_input(self, message: str) -> None:
        """Spawn one codex turn — exec on first turn, exec resume after."""
        if self._state.started_at is None:
            raise RuntimeError("send_input before start()")
        if self._state.status in {"done", "cancelled", "error"}:
            raise RuntimeError(
                f"session {self.session_id!r} is terminal ({self._state.status})"
            )

        await self._emit(
            {
                "type": "user_echo",
                "message_id": f"user-{self._state.seq}",
                "text": message,
            }
        )

        self._state.turn_count += 1
        cmd = self._build_cmd(message)
        spawn_env = self._build_env()

        logger.info(
            "codex-stream: spawning turn=%d session=%s thread=%s",
            self._state.turn_count,
            self.session_id,
            self._state.thread_id,
        )
        # See claude.py for why we override asyncio's 64 KB line buffer.
        # codex stream-json shares the same large-line shape (cortex_read
        # results inlined into tool_call_done envelopes etc.); without the
        # override, _read_stdout dies on the first overrun and the
        # session goes dark.
        self._process = await self._subprocess_factory(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=spawn_env,
            limit=16 * 1024 * 1024,
            # ROCK-SOLID v5 P4.1/P4.2 — own process group.
            #
            # Without this the subagent lives in the ENGINE's process group,
            # which makes the successor-engine reap unimplementable: a
            # process-group SIGTERM aimed at a subagent would take the engine
            # (and its siblings) with it. The legacy dispatcher.py path has
            # had start_new_session=True at its Popen for a long time; the
            # three streaming adapters — the path most subagents now take —
            # never did.
            #
            # It also stops a Ctrl-C / SIGINT to the engine's terminal being
            # broadcast to every live subagent.
            start_new_session=True,
        )

        # codex exec takes its prompt as a positional arg and does not read
        # stdin; with an open stdin pipe it prints "Reading additional input
        # from stdin..." and blocks. Close it so the turn proceeds.
        if self._process.stdin is not None:
            with contextlib.suppress(Exception):
                self._process.stdin.close()

        proc = self._process
        readers = [
            asyncio.create_task(
                self._read_stdout(proc),
                name=f"codex-{self.session_id}-stdout-{self._state.turn_count}",
            ),
            asyncio.create_task(
                self._read_stderr(proc),
                name=f"codex-{self.session_id}-stderr-{self._state.turn_count}",
            ),
        ]
        self._readers.extend(readers)
        await self._wait_turn(proc, readers)

    async def cancel(self) -> None:
        if self._closed:
            return

        if self._process is not None:
            try:
                self._process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=self._terminate_grace_sec
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "codex-stream: SIGTERM grace expired session=%s — SIGKILL",
                    self.session_id,
                )
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)

        if self._state.status not in {"done", "error"}:
            self._state.status = "cancelled"

        await self._emit(
            {
                "type": "status",
                "state": self._state.status,
                "detail": {"reason": "cancel-requested"},
            }
        )

        # No global MCP registration to remove — the inline server is
        # spawn-local (injected via `-c`), so it evaporates when the codex
        # process exits.
        self._mcp_attached = False

        await self._cleanup_readers()
        await self._event_queue.put(None)
        self._closed = True

    async def aclose(self) -> None:
        await self.cancel()

    # -- streaming surface ---------------------------------------------------

    async def stream(self) -> AsyncIterator[dict]:
        while True:
            evt = await self._event_queue.get()
            if evt is None:
                return
            yield evt

    def poll_events(self, since_seq: int = 0) -> list[dict]:
        if not self.transcript_path.exists():
            return []
        from .claude import UNIFIED_EVENT_TYPES

        events: list[dict] = []
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            if int(evt.get("seq", -1)) >= since_seq:
                if evt.get("type") in UNIFIED_EVENT_TYPES:
                    events.append(evt)
        return events

    # -- introspection -------------------------------------------------------

    @property
    def status(self) -> str:
        return self._state.status

    @property
    def started_at(self) -> Optional[str]:
        return self._state.started_at

    @property
    def ended_at(self) -> Optional[str]:
        return self._state.ended_at

    @property
    def last_event_at(self) -> Optional[str]:
        return self._state.last_event_at

    @property
    def seq(self) -> int:
        return self._state.seq

    @property
    def thread_id(self) -> Optional[str]:
        """Codex-assigned thread id captured from the first turn.

        ``None`` until the first ``thread.started`` event fires. The
        registry reads this after each ``send_input`` to persist the
        value to ``sessions_inline.provider_session_id``.
        """
        return self._state.thread_id

    @property
    def turn_count(self) -> int:
        return self._state.turn_count

    # -- internals -----------------------------------------------------------

    def _build_cmd(self, message: str) -> list[str]:
        """Render the per-turn codex command.

        First turn → ``codex exec``; subsequent turns →
        ``codex exec resume <thread_id>``. Both carry the sandbox
        backstop (workspace-write) and bypass codex's own approval/sandbox
        in favour of okuro's MCP middleware gate (contract §1.4).
        """
        if self._state.thread_id:
            base = [
                self.binary,
                "exec",
                "resume",
                self._state.thread_id,
            ]
        else:
            base = [self.binary, "exec"]

        base.extend(
            [
                "--json",
                "--sandbox",
                self.sandbox,
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
        )
        # Inline okuro MCP over streamable-HTTP. Two things codex needs that
        # the global `codex mcp add` path did NOT give us:
        #   1. experimental_use_rmcp_client=true — codex gates its
        #      streamable-HTTP MCP client behind this flag. Without it the
        #      client never completes the handshake to okuro's /mcp/v1 mount
        #      and the turn hangs >600s with zero tool calls.
        #   2. The server injected PER-SPAWN via `-c` (ephemeral, like
        #      claude's --mcp-config file) instead of a persistent
        #      `codex mcp add` entry in ~/.codex/config.toml. The global
        #      registry leaks one server per session forever and every codex
        #      turn then re-loads all of them. Per-spawn -c has no global
        #      state and no cross-session leak. The server key is
        #      session-unique so it never collides with the user's own
        #      `[mcp_servers.okuro]` stdio entry in the global config.
        if self.mcp_endpoint_url and self.mcp_bearer_token:
            server_key = inline_mcp_server_name(self.session_id)
            bearer_var = inline_bearer_env_name(self.session_id)
            base.extend(
                [
                    "-c",
                    "experimental_use_rmcp_client=true",
                    "-c",
                    f'mcp_servers.{server_key}.url="{self.mcp_endpoint_url}"',
                    "-c",
                    f'mcp_servers.{server_key}.bearer_token_env_var="{bearer_var}"',
                ]
            )
        # Codex tiers by reasoning EFFORT, not model name. When the resolved
        # "model" is a `model_reasoning_effort=<level>` directive, pass it via
        # `-c` and let codex use the account model from ~/.codex/config.toml
        # (auto-updates on subscription upgrade). A real model name still goes
        # via `-m`; empty → codex's own default.
        m = (self.model or "").strip()
        if m.startswith("model_reasoning_effort="):
            base.extend(["-c", m])
        elif m:
            base.extend(["-m", m])
        base.append(message)
        return base

    def _build_env(self) -> dict[str, str]:
        """Sanitised env for codex spawn.

        - Strip CODEX*-prefixed env so we don't nest.
        - Set the per-session bearer env var so codex can read it for
          MCP authentication (codex mcp add --bearer-token-env-var).
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CODEX")
        }
        if self.mcp_bearer_token:
            env[inline_bearer_env_name(self.session_id)] = self.mcp_bearer_token
        env.update(self._extra_env)
        return env

    async def _read_stdout(self, proc: Any) -> None:
        if proc.stdout is None:
            return
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "codex-stream: non-json stdout ignored session=%s line=%r",
                        self.session_id,
                        line[:200],
                    )
                    continue
                # Capture the codex thread id on first sight.
                if (
                    isinstance(parsed, dict)
                    and parsed.get("type") == "thread.started"
                    and parsed.get("thread_id")
                    and not self._state.thread_id
                ):
                    self._state.thread_id = parsed.get("thread_id")
                events = translate_codex_event(
                    parsed,
                    seq=self._state.seq,
                    session_id=self.session_id,
                    ts=_iso_now(),
                )
                for evt in events:
                    await self._emit(evt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "codex-stream: stdout reader crashed session=%s", self.session_id
            )

    async def _read_stderr(self, proc: Any) -> None:
        if proc.stderr is None:
            return
        try:
            async for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                await self._append_transcript_raw(
                    {
                        "_envelope": "stderr",
                        "session_id": self.session_id,
                        "ts": _iso_now(),
                        "line": line,
                    }
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "codex-stream: stderr reader crashed session=%s", self.session_id
            )

    async def _wait_turn(self, proc: Any, readers: list[asyncio.Task]) -> None:
        try:
            rc = await proc.wait()
        except Exception:  # noqa: BLE001
            rc = -1
        for task in readers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=1.0)
        self._process = None
        if rc not in (0, -15):
            await self._emit(
                {
                    "type": "error",
                    "code": "turn_failed",
                    "message": f"codex turn exited rc={rc}",
                    "recoverable": True,
                }
            )

    async def _cleanup_readers(self) -> None:
        for task in list(self._readers):
            if task is asyncio.current_task():
                continue
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)

    async def _emit(self, payload: dict) -> None:
        if "seq" not in payload:
            payload["seq"] = self._state.seq
        if "ts" not in payload:
            payload["ts"] = _iso_now()
        if "session_id" not in payload:
            payload["session_id"] = self.session_id
        self._state.seq = int(payload["seq"]) + 1
        self._state.last_event_at = payload["ts"]

        if payload.get("type") == "status":
            state = payload.get("state")
            if state in {"done", "cancelled", "error", "paused", "running"}:
                self._state.status = state
                if state in {"done", "cancelled", "error"} and not self._state.ended_at:
                    self._state.ended_at = payload["ts"]

        await self._append_transcript_raw(payload)
        await self._event_queue.put(payload)
        for cb in list(self._on_event_callbacks):
            try:
                cb(payload)
            except Exception:  # noqa: BLE001
                logger.exception("codex-stream: callback error session=%s", self.session_id)

    async def _append_transcript_raw(self, record: dict) -> None:
        async with self._transcript_lock:
            try:
                line = json.dumps(record, default=str)
            except (TypeError, ValueError):
                line = json.dumps(
                    {
                        "_envelope": "unencodable",
                        "session_id": self.session_id,
                        "ts": _iso_now(),
                    }
                )
            with self.transcript_path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

    def on_event(self, callback: Any) -> None:
        self._on_event_callbacks.append(callback)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
