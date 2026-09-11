# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: AntigravityStreamAdapter — spawn-per-turn agy CLI subprocess + unified-event translator.
# index:
#   imports
#   def translate_antigravity_line
#   def inline_mcp_server_name
#   def session_home_dir
#   def write_session_mcp_config
#   class AntigravityStreamAdapter
# AGENT_HEADER_END -->
"""Antigravity CLI (``agy``) streaming adapter for inline web sessions.

``agy`` reaches Google's Gemini models — plus Claude and GPT-OSS — on a
consumer subscription. It replaced the gemini CLI as okuro's Google path
when that CLI stopped serving individual accounts on 2026-06-18.

Everything below was measured against **agy 1.1.4** on 2026-07-18, not
inferred from the contract doc. The three findings that shape this
adapter:

1. **No structured output.** ``agy`` has no ``--output-format``; print
   mode emits prose on stdout, nothing else. There are no tool-call
   events, no usage records, and no token deltas to be had. We forward
   stdout LINES as ``token`` events as they arrive — real data, one
   event per line. We never synthesise a fake delta stream, and we
   never fabricate ``tool_call_start`` / ``usage`` events the CLI
   did not give us. A turn therefore renders as
   ``message_start`` → N × ``token`` → ``message_stop``.

2. **No per-invocation MCP flag.** Unlike codex (``-c mcp_servers…``)
   or claude (``--mcp-config <path>``), ``agy`` has no MCP subcommand
   and no config-override flag. Its only config seam is ``$HOME``:
   it reads ``$HOME/.gemini/config/mcp_config.json``. So each session
   gets a private HOME under the okuro sessions dir, and we write the
   inline server into it. Nothing in the user's real ``~/.gemini`` is
   read, written, or mutated, and concurrent sessions cannot collide.

3. **agy speaks HTTP MCP.** Verified by pointing it at a local probe
   listener: it POSTs to the configured URL with the
   ``Authorization: Bearer …`` header from ``headers``, then falls back
   to OAuth discovery on 401. So the session-scoped bearer model used
   by the other adapters works here unchanged — the token goes in the
   config file's ``headers``, never on the command line (argv is world
   readable via /proc).

**OKURO_HOME MUST be pinned in the spawn env.** This is not a nicety.
okuro resolves its own home from ``$OKURO_HOME`` and falls back to
``$HOME/.okuro``; handing an agent a blank HOME with okuro's MCP
attached and ``--dangerously-skip-permissions`` set caused a 141 GB
clone of the real ``~/.okuro`` (including okuro.db) into the temp dir
during development. :meth:`_build_env` pins it and
``tests/bridge/streaming/test_antigravity_home_isolation.py`` asserts
it. Do not remove that test.

Per-turn invocation::

    agy -p "<user message>"
        --dangerously-skip-permissions
        --model "<display name>"
        [--conversation <id>]        # turns 2..n

``--dangerously-skip-permissions`` is the ``--yolo`` equivalent and is
intentional: okuro gates tool calls at the MCP middleware tier, not at
the CLI. Contract §1.4.

Model names are **display strings** with spaces and parens
("Gemini 3.1 Pro (High)") — passed as one argv element, never shell
interpolated. ``agy models`` lists the live set.

Conversation continuity: ``agy`` stores conversations under
``$HOME/.gemini/antigravity-cli/conversations``. Because the session
HOME is stable across turns, ``--continue`` resumes the same thread
without us tracking an id. We still capture ``--conversation`` support
via :attr:`thread_id` for a future rehydrate path.

Latency: a bare turn measured 10–16 s; a turn that actually exercises
okuro MCP tools measured >100 s (okuro exposes 261 tools and agy loads
them per spawn). The registry already gives non-claude providers a
600 s start budget — do not tighten it for this adapter.

Stderr discipline matches claude/codex: tee to the transcript under an
``stderr`` envelope, never onto the unified event stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event translation
# ---------------------------------------------------------------------------


def translate_antigravity_line(
    line: str, *, seq: int, session_id: str, ts: str, message_id: str
) -> list[dict]:
    """Translate one stdout line into unified events.

    Pure function: no I/O, no clock reads — mirrors the claude / codex
    translators so the same test shape applies.

    ``agy`` gives us prose, so there is exactly one honest mapping: the
    line becomes a ``token``. Callers own the surrounding
    ``message_start`` / ``message_stop`` bracketing because those depend
    on turn lifecycle, not on line content.

    Returns an empty list for blank lines so the caller can forward
    stdout verbatim without pre-filtering.
    """
    if not line.strip():
        return []
    return [
        {
            "type": "token",
            "seq": seq,
            "ts": ts,
            "session_id": session_id,
            "message_id": message_id,
            "text": line,
        }
    ]


# ---------------------------------------------------------------------------
# Per-session HOME + MCP config
# ---------------------------------------------------------------------------


def _short_session(session_id: str) -> str:
    return session_id[:8]


def inline_mcp_server_name(session_id: str) -> str:
    """Session-unique MCP server key.

    Unique per session even though each session has a private HOME —
    the name shows up in agy's logs and in okuro's MCP access records,
    so a collision-free name keeps those readable.
    """
    return f"okuro-inline-{_short_session(session_id)}"


def session_home_dir(session_id: str, *, okuro_home: Optional[Path] = None) -> Path:
    """Private ``$HOME`` for one agy session.

    Lives under the okuro sessions dir (not /tmp) so it shares the
    transcript's lifecycle and backup posture.
    """
    base = okuro_home or _okuro_home()
    return base / "sessions" / f"agy-home-{session_id}"


def _okuro_home() -> Path:
    home_env = os.environ.get("OKURO_HOME")
    if home_env:
        return Path(home_env)
    return okuro_home()


def write_session_mcp_config(
    home: Path,
    *,
    session_id: str,
    endpoint_url: str,
    bearer_token: str,
) -> Path:
    """Write the inline okuro MCP server into a session's private HOME.

    agy reads ``$HOME/.gemini/config/mcp_config.json``. The bearer goes
    in ``headers`` — a file we chmod 0600 — rather than on argv, which
    is world-readable through /proc.

    Returns the config path written.
    """
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "mcp_config.json"
    payload = {
        "mcpServers": {
            inline_mcp_server_name(session_id): {
                "url": endpoint_url,
                "headers": {"Authorization": f"Bearer {bearer_token}"},
            }
        }
    }
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        config_path.chmod(0o600)
    return config_path


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _StreamState:
    """Mutable runtime state for one antigravity adapter.

    Duplicated from claude / codex by design — three small dataclasses
    is clearer than one shared with optional fields.
    """

    seq: int = 0
    status: str = "running"
    last_event_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    thread_id: Optional[str] = None
    turn_count: int = 0


class AntigravityStreamAdapter:
    """Spawn-per-turn ``agy`` subprocess + unified-event translator.

    Lifecycle:
      * ``start()`` — primes the transcript, builds the private HOME and
        writes the session-scoped MCP config into it.
      * ``send_input(msg)`` — spawns one ``agy -p`` turn; turns 2..n add
        ``--continue`` so agy resumes the conversation stored in the
        session HOME.
      * ``cancel()`` — SIGTERM/SIGKILL any in-flight subprocess, remove
        the private HOME, push the terminal sentinel.

    Public protocol matches claude + codex.
    """

    DEFAULT_BINARY = "agy"
    DEFAULT_MODEL = "Gemini 3.5 Flash (High)"

    def __init__(
        self,
        *,
        session_id: str,
        model: str = DEFAULT_MODEL,
        system_prompt: Optional[str] = None,
        transcript_path: Path,
        mcp_endpoint_url: Optional[str] = None,
        mcp_bearer_token: Optional[str] = None,
        binary: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        subprocess_factory: Optional[Any] = None,
        terminate_grace_sec: float = 2.0,
        home_dir: Optional[Path] = None,
        keep_home: bool = False,
    ) -> None:
        self.session_id = session_id
        self.model = model or self.DEFAULT_MODEL
        self.system_prompt = system_prompt
        self.transcript_path = transcript_path
        self.mcp_endpoint_url = mcp_endpoint_url
        self.mcp_bearer_token = mcp_bearer_token
        self.binary = binary or self.DEFAULT_BINARY
        self.cwd = cwd
        self._extra_env = dict(env or {})
        self._subprocess_factory = subprocess_factory or asyncio.create_subprocess_exec
        self._terminate_grace_sec = terminate_grace_sec
        # keep_home leaves the private HOME on disk after cancel — for
        # debugging a session's agy logs. Off by default: the dir holds a
        # bearer token.
        self._keep_home = keep_home
        self.home_dir = home_dir or session_home_dir(session_id)

        self._state = _StreamState()
        self._process: Any = None
        self._event_queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        self._readers: list[asyncio.Task] = []
        self._transcript_lock = asyncio.Lock()
        self._on_event_callbacks: list[Any] = []
        self._closed = False
        self._mcp_attached = False
        self._current_message_id: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._state.started_at is not None:
            raise RuntimeError(
                f"AntigravityStreamAdapter for session {self.session_id!r} already started"
            )

        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.transcript_path.exists():
            self.transcript_path.touch(mode=0o600)

        self._state.started_at = _iso_now()
        self._state.last_event_at = self._state.started_at

        # Private HOME is the ONLY way to scope agy's MCP surface — it has
        # no per-invocation config flag. Build it before the first spawn.
        self.home_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.home_dir.chmod(0o700)

        if self.mcp_endpoint_url and self.mcp_bearer_token:
            write_session_mcp_config(
                self.home_dir,
                session_id=self.session_id,
                endpoint_url=self.mcp_endpoint_url,
                bearer_token=self.mcp_bearer_token,
            )
            self._mcp_attached = True

        await self._emit(
            {
                "type": "status",
                "state": "running",
                "detail": {
                    "model": self.model,
                    "provider": "antigravity",
                    "mcp_attached": self._mcp_attached,
                    "home_dir": str(self.home_dir),
                },
            }
        )

    async def send_input(self, message: str) -> None:
        """Spawn one agy turn. Blocks until the turn completes."""
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
        message_id = f"asst-{self.session_id[:8]}-{self._state.turn_count}"
        self._current_message_id = message_id

        cmd = self._build_cmd(message)
        spawn_env = self._build_env()

        logger.info(
            "agy-stream: spawning turn=%d session=%s model=%r",
            self._state.turn_count,
            self.session_id,
            self.model,
        )

        await self._emit(
            {
                "type": "message_start",
                "message_id": message_id,
                "role": "assistant",
            }
        )

        # Large limit for parity with claude/codex: an agy answer that
        # inlines a cortex_read result arrives as one very long line and
        # would otherwise blow asyncio's 64 KB default and kill the reader.
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

        # agy takes its prompt via -p and does not read stdin. Leaving the
        # pipe open risks the same "waiting for stdin" stall codex hits.
        if self._process.stdin is not None:
            with contextlib.suppress(Exception):
                self._process.stdin.close()

        proc = self._process
        readers = [
            asyncio.create_task(
                self._read_stdout(proc),
                name=f"agy-{self.session_id}-stdout-{self._state.turn_count}",
            ),
            asyncio.create_task(
                self._read_stderr(proc),
                name=f"agy-{self.session_id}-stderr-{self._state.turn_count}",
            ),
        ]
        self._readers.extend(readers)
        await self._wait_turn(proc, readers)

        await self._emit(
            {
                "type": "message_stop",
                "message_id": message_id,
            }
        )
        self._current_message_id = None

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
                    "agy-stream: SIGTERM grace expired session=%s — SIGKILL",
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

        # The private HOME holds the session bearer in a config file — drop
        # it with the session unless explicitly retained for debugging.
        self._detach_home()
        self._mcp_attached = False

        await self._cleanup_readers()
        await self._event_queue.put(None)
        self._closed = True

    async def aclose(self) -> None:
        await self.cancel()

    def _detach_home(self) -> None:
        if self._keep_home:
            logger.info(
                "agy-stream: keeping session HOME session=%s path=%s (holds a bearer)",
                self.session_id,
                self.home_dir,
            )
            return
        try:
            shutil.rmtree(self.home_dir)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(
                "agy-stream: could not remove session HOME session=%s path=%s",
                self.session_id,
                self.home_dir,
            )

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
        """Always ``None`` for agy.

        Present so the registry's ``getattr(adapter, "thread_id", None)``
        probe behaves uniformly. agy has no externally visible thread id:
        continuity comes from the stable session HOME plus ``--continue``,
        not from an id we could persist.
        """
        return self._state.thread_id

    @property
    def turn_count(self) -> int:
        return self._state.turn_count

    # -- internals -----------------------------------------------------------

    def _build_cmd(self, message: str) -> list[str]:
        """Render the per-turn agy command.

        Turn 1 starts fresh; turns 2..n add ``--continue`` so agy picks up
        the conversation it stored in this session's private HOME.

        The model is a display string with spaces ("Gemini 3.1 Pro
        (High)") — it goes in as a single argv element, so there is no
        quoting concern. An empty model means "let agy choose".
        """
        cmd = [self.binary, "-p", message, "--dangerously-skip-permissions"]

        model = (self.model or "").strip()
        if model:
            cmd.extend(["--model", model])

        if self._state.turn_count > 1:
            cmd.append("--continue")

        return cmd

    def _build_env(self) -> dict[str, str]:
        """Sanitised env for the agy spawn.

        Three things happen here, and the second is load-bearing:

        1. ``HOME`` points at the session's private dir — the only seam
           agy gives us for scoping its MCP surface.
        2. ``OKURO_HOME`` is pinned to the REAL okuro home. Without this,
           okuro's MCP server resolves its home under the fake HOME,
           finds nothing, and seeds itself from the real one — a 141 GB
           copy of ~/.okuro (okuro.db included) was produced this way
           during development. Never remove this line.
        3. ``GEMINI_*`` / ``ANTIGRAVITY_*`` env is stripped so an agy
           session that spawned us cannot nest its own config into ours.
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GEMINI", "ANTIGRAVITY"))
        }
        env["HOME"] = str(self.home_dir)
        env["OKURO_HOME"] = str(_okuro_home())
        env["OKURO_PROVIDER"] = "antigravity"
        env.update(self._extra_env)
        return env

    async def _read_stdout(self, proc: Any) -> None:
        """Forward stdout lines as token events.

        agy emits prose, so every non-blank line is real model output.
        We do not attempt to parse structure that is not there.
        """
        if proc.stdout is None:
            return
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                events = translate_antigravity_line(
                    line,
                    seq=self._state.seq,
                    session_id=self.session_id,
                    ts=_iso_now(),
                    message_id=self._current_message_id or "asst-unknown",
                )
                for evt in events:
                    await self._emit(evt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "agy-stream: stdout reader crashed session=%s", self.session_id
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
                "agy-stream: stderr reader crashed session=%s", self.session_id
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
                    "message": f"agy turn exited rc={rc}",
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
                logger.exception("agy-stream: callback error session=%s", self.session_id)

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
