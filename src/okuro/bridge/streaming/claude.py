# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: ClaudeStreamAdapter — long-lived claude CLI subprocess + unified-event translator.
# index:
#   imports
#   UNIFIED_EVENT_TYPES
#   def translate_claude_event
#   class ClaudeStreamAdapter
# AGENT_HEADER_END -->
"""Claude CLI streaming adapter for inline web sessions.

The adapter spawns ``claude -p --include-partial-messages --input-format
stream-json --output-format stream-json --mcp-config <path>`` and shuttles
events between the CLI's stdio and the okuro event protocol defined in
``docs/research/cli-streaming-contract.md`` §2.

Why a long-lived process for claude (vs spawn-per-turn for gemini/codex):
claude alone supports multi-turn input over stdin via ``--input-format
stream-json``. See contract §1.5 and §7 recommendation B. Wave 2 only
ships claude; the gemini/codex spawn-per-turn variants are wave 3.

stderr discipline: every line read from the child's stderr is appended to
the on-disk transcript (with an ``stderr`` envelope marker) and NEVER
forwarded to the unified event stream. The contract calls this out in
§1.5 — gemini emits noisy non-fatal stderr in non-TTY mode and would
poison the SSE channel; we apply the same discipline to every adapter.

Cancel discipline: SIGTERM first with a 2-second grace, then SIGKILL.
The contract suggests 5s grace at the SSE/lifecycle layer; the adapter
itself uses 2s because at the adapter level we want the registry to
observe the terminal state quickly — the longer drain window lives in
:meth:`StreamRegistry.cancel`.

Transcript layout (wave 2):
  ``<okuro_home>/sessions/<session_id>.jsonl`` — one JSON event per line.
The ``sessions_inline.transcript_path`` column stores the RELATIVE path
``sessions/<session_id>.jsonl`` so the row stays portable across
``OKURO_HOME`` moves (contract §5). Contract §5 calls for a
year/month/day partitioned layout under ``sessions/inline/`` — wave 2
ships the flat layout per the brief; the partitioned tree is a wave-4
follow-up tracked in this module's TODO(wave-4) comments.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid


def _as_uuid_dashed(sid: str) -> str:
    """Normalise an okuro session_id (hex32) to a hyphenated UUID claude accepts."""
    try:
        return str(uuid.UUID(sid))
    except ValueError:
        return sid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unified event protocol — names from contract §2.1
# ---------------------------------------------------------------------------

UNIFIED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "status",
        "message_start",
        "token",
        "message_stop",
        "user_echo",
        "tool_call_start",
        "tool_approval_required",
        "tool_call_done",
        "usage",
        "error",
    }
)
"""Discriminator values the unified ``type`` field may take.

The contract doc §2.1 is the source of truth. Any change here implies a
matching schema update plus a wave bump for downstream consumers (web
SSE renderer + transcript ingester).

``tool_approval_required`` is reserved — wave 2 never emits it because
the approval gate is wave-3 work. It stays in the vocabulary so the
typed-union code generated for the web SSE renderer doesn't need to be
re-bumped when wave 3 lands.
"""


# ---------------------------------------------------------------------------
# Translator (pure function — easy to unit-test in isolation)
# ---------------------------------------------------------------------------


def translate_claude_event(raw: dict, *, seq: int, session_id: str, ts: str) -> list[dict]:
    """Translate one claude CLI stream-json line into unified events.

    The translator is intentionally pure (no I/O, no clock reads, no
    side effects): the caller is responsible for sequencing ``seq``,
    minting ``ts`` and injecting ``session_id``. This makes the
    translator trivially testable from a captured JSONL fixture.

    One claude line can fan out into multiple unified events. The most
    common fan-out is an ``assistant`` message whose ``content`` carries
    both a text block AND one or more ``tool_use`` blocks — we emit one
    ``message_start`` + one ``message_stop`` for the text payload AND one
    ``tool_call_start`` per ``tool_use``.

    Unknown / future event types degrade to no-ops (empty list). The
    raw line is still persisted to the transcript by the caller, so
    nothing is lost — but we do not invent shapes the contract has not
    defined.
    """
    if not isinstance(raw, dict):
        return []

    kind = raw.get("type")
    out: list[dict] = []

    def _envelope(payload: dict) -> dict:
        # Order matters for the on-disk transcript readability (type
        # first, then session/seq, then payload-specific keys). Python
        # 3.7+ preserves dict insertion order — relied on elsewhere in
        # the codebase (cf. write_memory record format).
        evt: dict[str, Any] = {
            "type": payload["type"],
            "session_id": session_id,
            "seq": seq + len(out),  # stable across the fan-out
            "ts": ts,
        }
        for key, value in payload.items():
            if key == "type":
                continue
            evt[key] = value
        return evt

    # --- system.init ----------------------------------------------------
    if kind == "system" and raw.get("subtype") == "init":
        out.append(
            _envelope(
                {
                    "type": "status",
                    "state": "running",
                    "detail": {
                        "model": raw.get("model"),
                        "cwd": raw.get("cwd"),
                        "mcp_servers": raw.get("mcp_servers", []),
                        "tools": raw.get("tools", []),
                        "claude_session_id": raw.get("session_id"),
                    },
                }
            )
        )
        return out

    # --- stream_event (partial-message deltas) --------------------------
    # `--include-partial-messages` wraps anthropic SSE deltas. We only
    # care about content_block_delta (text token stream) and
    # message_start / message_stop for the assistant role. Other shapes
    # (ping, message_delta usage updates) become no-ops here — they are
    # captured in the transcript via the caller.
    if kind == "stream_event":
        sub = raw.get("event") or {}
        sub_type = sub.get("type")
        if sub_type == "message_start":
            msg = sub.get("message") or {}
            out.append(
                _envelope(
                    {
                        "type": "message_start",
                        "message_id": msg.get("id"),
                        "role": msg.get("role", "assistant"),
                    }
                )
            )
        elif sub_type == "content_block_delta":
            delta = sub.get("delta") or {}
            text = delta.get("text") or ""
            if text:
                out.append(
                    _envelope(
                        {
                            "type": "token",
                            "message_id": sub.get("message_id")
                            or (sub.get("message") or {}).get("id"),
                            "delta": text,
                        }
                    )
                )
        elif sub_type == "message_stop":
            # Final-text variant of message_stop is emitted from the
            # outer ``assistant`` envelope below — this branch just
            # records that the partial-stream is done; we suppress to
            # avoid duplicate stops.
            pass
        return out

    # --- assistant (final message, may carry tool_use) ------------------
    if kind == "assistant":
        msg = raw.get("message") or {}
        message_id = msg.get("id")
        content = msg.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_calls.append(block)
        # Suppress the stop event for content-less envelopes (claude
        # emits empty assistants between tool_use and tool_result). The
        # downstream UI does not need to render an empty bubble.
        if text_parts:
            out.append(
                _envelope(
                    {
                        "type": "message_stop",
                        "message_id": message_id,
                        "text": "".join(text_parts),
                        "stop_reason": msg.get("stop_reason"),
                    }
                )
            )
        for tu in tool_calls:
            # TODO(wave-3): once the approval gate ships, the registry
            # will intercept here and emit ``tool_approval_required``
            # before forwarding the start. For wave 2 every tool_use is
            # auto-flagged (tier defaults to ``read``).
            out.append(
                _envelope(
                    {
                        "type": "tool_call_start",
                        "call_id": tu.get("id"),
                        "tool_name": tu.get("name"),
                        "args": tu.get("input") or {},
                        "tier": "read",
                        "mcp_server": None,
                    }
                )
            )
        usage = msg.get("usage") or {}
        if usage:
            out.append(
                _envelope(
                    {
                        "type": "usage",
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        # Cache columns are the load-bearing half of per-spawn
                        # cost: cache reads bill at ~10% of the input rate, so
                        # input_tokens alone overstates a warm spawn by an order
                        # of magnitude. Surfaced here so `spawn_usage` can carry
                        # effective billable input rather than a naive count.
                        "cache_read_input_tokens": usage.get(
                            "cache_read_input_tokens", 0
                        ),
                        "cache_creation_input_tokens": usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                        "cost_usd": 0,  # subscription — contract §9
                    }
                )
            )
        return out

    # --- user (tool_result echoes) -------------------------------------
    if kind == "user":
        msg = raw.get("message") or {}
        content = msg.get("content") or []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    # claude often nests {"type":"text","text":"..."} inside
                    text_pieces: list[str] = []
                    for piece in result:
                        if isinstance(piece, dict) and piece.get("type") == "text":
                            text_pieces.append(piece.get("text") or "")
                    result_text: Any = "".join(text_pieces) if text_pieces else result
                else:
                    result_text = result
                out.append(
                    _envelope(
                        {
                            "type": "tool_call_done",
                            "call_id": block.get("tool_use_id"),
                            "outcome": "error" if block.get("is_error") else "auto",
                            "result": result_text,
                            "error": (result_text if block.get("is_error") else None),
                            "duration_ms": None,
                        }
                    )
                )
        return out

    # --- result (turn / session terminal) ------------------------------
    if kind == "result":
        is_error = bool(raw.get("is_error"))
        # The AUTHORITATIVE usage for the whole session. Per-assistant-message
        # usage above is per message; this is the CLI's own cumulative total,
        # and it is the only place total_cost_usd exists. Emitted as a `usage`
        # event marked `final` so the dispatcher prefers it over the running
        # sum instead of double-counting.
        _usage = raw.get("usage") or {}
        if _usage or raw.get("total_cost_usd"):
            out.append(
                _envelope(
                    {
                        "type": "usage",
                        "final": True,
                        "input_tokens": _usage.get("input_tokens", 0),
                        "output_tokens": _usage.get("output_tokens", 0),
                        "cache_read_input_tokens": _usage.get(
                            "cache_read_input_tokens", 0
                        ),
                        "cache_creation_input_tokens": _usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                        "cost_usd": raw.get("total_cost_usd") or 0,
                        "duration_ms": raw.get("duration_ms") or 0,
                    }
                )
            )
        out.append(
            _envelope(
                {
                    "type": "status",
                    "state": "error" if is_error else "done",
                    "detail": {
                        "duration_ms": raw.get("duration_ms"),
                        "num_turns": raw.get("num_turns"),
                        "result": raw.get("result"),
                        "subtype": raw.get("subtype"),
                    },
                }
            )
        )
        return out

    # Anything else (future event shape) → no unified event but caller
    # still records the raw line. Don't invent.
    return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _StreamState:
    """Mutable runtime state for one adapter.

    Lifted into a dataclass so tests can introspect / reset cleanly.
    """

    seq: int = 0
    status: str = "running"  # mirrors sessions_inline.status vocabulary
    pending_user_messages: list[str] = field(default_factory=list)
    last_event_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    claude_session_id: Optional[str] = None


class ClaudeStreamAdapter:
    """Long-lived claude CLI subprocess + unified-event translator.

    Usage:

        adapter = ClaudeStreamAdapter(
            session_id="abc",
            model="sonnet",
            transcript_path=Path("/tmp/abc.jsonl"),
        )
        await adapter.start()
        await adapter.send_input("hello")
        async for evt in adapter.stream():
            ...
        await adapter.cancel()

    Tests substitute ``subprocess_factory`` to inject a fake subprocess
    that yields canned stdout/stderr lines without invoking a real
    claude binary.
    """

    DEFAULT_BINARY = "claude"
    # Wave 3a wires the inline HTTP MCP target (contract §4.2 Option A).
    # When the registry does not provide endpoint+bearer (legacy callers,
    # smoke tests), we fall back to an empty servers map so the CLI
    # spawns without any MCP wiring rather than failing the start.
    EMPTY_MCP_CONFIG = '{"mcpServers": {}}'

    def __init__(
        self,
        *,
        session_id: str,
        model: str = "sonnet",
        system_prompt: Optional[str] = None,
        transcript_path: Path,
        mcp_config_path: Optional[Path] = None,
        mcp_endpoint_url: Optional[str] = None,
        mcp_bearer_token: Optional[str] = None,
        binary: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        subprocess_factory: Optional[Any] = None,
        terminate_grace_sec: float = 2.0,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.system_prompt = system_prompt
        self.transcript_path = transcript_path
        self.mcp_config_path = mcp_config_path
        self.mcp_endpoint_url = mcp_endpoint_url
        self.mcp_bearer_token = mcp_bearer_token
        self.binary = binary or self.DEFAULT_BINARY
        self.cwd = cwd
        self._extra_env = dict(env or {})
        self._subprocess_factory = subprocess_factory or asyncio.create_subprocess_exec
        self._terminate_grace_sec = terminate_grace_sec

        self._state = _StreamState()
        self._process: Any = None
        self._event_queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        self._readers: list[asyncio.Task] = []
        self._transcript_lock = asyncio.Lock()
        self._on_event_callbacks: list[Any] = []
        # Sentinel that .stream() yields when no events are pending but
        # the process is still alive — drained shape lets the registry
        # poll-pump cleanly.
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the claude subprocess and start reader tasks.

        Idempotent: calling start twice on the same adapter raises (a
        stream session is single-use; create a new adapter to retry).
        """
        if self._process is not None:
            raise RuntimeError(
                f"ClaudeStreamAdapter for session {self.session_id!r} already started"
            )

        # Ensure transcript directory exists with restrictive permissions
        # — the transcript may carry tool args containing prompts or
        # secrets (contract §5).
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file with 0600 so existing umask doesn't widen it.
        if not self.transcript_path.exists():
            self.transcript_path.touch(mode=0o600)
        # Resolve / create mcp_config_path on demand.
        mcp_arg = self._resolve_mcp_config()

        cmd = [
            self.binary,
            "-p",
            "--verbose",
            "--include-partial-messages",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--mcp-config",
            str(mcp_arg),
            "--session-id",
            _as_uuid_dashed(self.session_id),
            "--permission-mode",
            "bypassPermissions",
            "--model",
            self.model,
        ]
        if self.system_prompt:
            cmd.extend(["--system-prompt", self.system_prompt])

        spawn_env = self._build_env()

        self._state.started_at = _iso_now()
        self._state.last_event_at = self._state.started_at

        logger.info(
            "claude-stream: spawning session=%s model=%s", self.session_id, self.model
        )
        # asyncio's default StreamReader limit is 64 KB per line. Claude's
        # stream-json regularly emits single lines well above that — large
        # tool_use args, long assistant text deltas, ~70 KB cortex_read
        # results inlined into a tool_call_done envelope. When a line
        # overruns the limit, ``async for raw_line in stdout`` raises
        # ``LimitOverrunError`` and the iterator exits silently; the
        # ``_read_stdout`` task then dies, the transcript stops growing,
        # and the dispatcher_streaming events poll has nothing to return.
        # The subagent keeps running (its stdin is still open) but every
        # downstream observer goes dark — exactly the 4.2 hang pattern.
        # 16 MB covers any realistic single line; reads still stream so
        # the per-line memory cost is bounded by actual content.
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

        # Reader tasks — one for stdout (translates), one for stderr
        # (transcript-only). They die when the streams hit EOF; the
        # process-watcher task observes the proc's wait() and pushes a
        # terminal status event into the queue.
        self._readers = [
            asyncio.create_task(self._read_stdout(), name=f"claude-{self.session_id}-stdout"),
            asyncio.create_task(self._read_stderr(), name=f"claude-{self.session_id}-stderr"),
            asyncio.create_task(self._watch_process(), name=f"claude-{self.session_id}-watch"),
        ]

    async def send_input(self, message: str) -> None:
        """Push one user message into the CLI's stdin.

        Wraps the text in claude's stream-json input envelope (one
        ``user`` message per line). The CLI replies with a fresh
        ``assistant`` chain on stdout.
        """
        if self._process is None:
            raise RuntimeError("send_input before start()")
        if self._state.status in {"done", "cancelled", "error"}:
            raise RuntimeError(
                f"session {self.session_id!r} is terminal ({self._state.status})"
            )

        envelope = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": message}],
            },
        }
        line = (json.dumps(envelope) + "\n").encode("utf-8")
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(line)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.warning(
                "claude-stream: stdin closed for session=%s (%s)", self.session_id, exc
            )
            await self._emit_error("stdin_closed", str(exc), recoverable=False)

        # User echo is contract-mandated (§2.1) — the CLI doesn't echo
        # the user message back, so we synthesize one.
        await self._emit(
            {
                "type": "user_echo",
                "message_id": f"user-{self._state.seq}",
                "text": message,
            }
        )

    async def cancel(self) -> None:
        """Stop the subprocess. SIGTERM with grace, then SIGKILL.

        Idempotent — calling cancel on an already-terminal session is a
        no-op (the status row is the source of truth, set once).
        """
        if self._closed:
            return
        if self._process is None:
            self._closed = True
            return

        # Signal the closed-down state to readers — they short-circuit on
        # the next loop iteration.
        if self._state.status not in {"done", "error"}:
            self._state.status = "cancelled"

        try:
            self._process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            # Already dead — nothing to do.
            pass
        try:
            await asyncio.wait_for(
                self._process.wait(), timeout=self._terminate_grace_sec
            )
        except asyncio.TimeoutError:
            logger.warning(
                "claude-stream: SIGTERM grace expired for session=%s — escalating to SIGKILL",
                self.session_id,
            )
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=2.0)

        await self._emit(
            {
                "type": "status",
                "state": self._state.status,
                "detail": {"reason": "cancel-requested"},
            }
        )
        await self._cleanup_readers()
        self._closed = True

    async def aclose(self) -> None:
        """Best-effort cleanup; calls cancel() if not already terminal."""
        await self.cancel()

    # -- streaming surface ---------------------------------------------------

    async def stream(self) -> AsyncIterator[dict]:
        """Yield unified events until the session reaches a terminal status.

        The async iterator drains the internal queue, which is fed by the
        stdout reader + the watch-process task. The watch task pushes a
        sentinel ``None`` after writing the final status event so the
        iterator can break cleanly.
        """
        while True:
            evt = await self._event_queue.get()
            if evt is None:
                return
            yield evt

    def poll_events(self, since_seq: int = 0) -> list[dict]:
        """Synchronous reader of buffered events from disk.

        The polling MCP tool ``bridge_stream_event`` reads the
        transcript instead of draining the async queue — this keeps the
        adapter's queue purely the SSE/web channel (wave 4) and avoids
        cross-coroutine ownership.
        """
        if not self.transcript_path.exists():
            return []
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
                # Only surface unified-event rows; stderr / raw rows
                # carry an ``_envelope`` marker that the MCP tool layer
                # filters out.
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
    def claude_session_id(self) -> Optional[str]:
        return self._state.claude_session_id

    @property
    def seq(self) -> int:
        return self._state.seq

    # -- internals -----------------------------------------------------------

    def _render_mcp_config(self) -> str:
        """Return the JSON body of the --mcp-config file.

        Wave 3a — when the registry provided an inline HTTP MCP endpoint
        + bearer, render a Streamable-HTTP server entry pointing at it
        with the bearer in the Authorization header. The CLI's MCP client
        then routes every okuro tool call back through the FastAPI mount
        which runs the approval gate. Without an endpoint we fall back
        to an empty map so the spawn doesn't fail.
        """
        if not self.mcp_endpoint_url or not self.mcp_bearer_token:
            return self.EMPTY_MCP_CONFIG
        config = {
            "mcpServers": {
                "okuro": {
                    "type": "http",
                    "url": self.mcp_endpoint_url,
                    "headers": {
                        "Authorization": f"Bearer {self.mcp_bearer_token}"
                    },
                }
            }
        }
        return json.dumps(config)

    def _resolve_mcp_config(self) -> Path:
        """Resolve --mcp-config path, materializing the body on disk.

        The body comes from :meth:`_render_mcp_config`. The file mode is
        0600 because the bearer token is plaintext on disk.
        """
        body = self._render_mcp_config()
        if self.mcp_config_path is not None:
            self.mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
            self.mcp_config_path.write_text(body)
            try:
                self.mcp_config_path.chmod(0o600)
            except OSError:
                pass
            return self.mcp_config_path

        default_dir = self.transcript_path.parent.parent / "mcp"
        default_dir.mkdir(parents=True, exist_ok=True)
        target = default_dir / f"{self.session_id}.json"
        target.write_text(body)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target

    def _build_env(self) -> dict[str, str]:
        """Return a sanitised env dict for the spawned claude.

        Mirrors :mod:`okuro.bridge.executor` — strip CLAUDE-prefixed
        env vars so the spawned claude does not bootstrap into a
        nested okuro session, then re-inject the caller's overrides.
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CLAUDE") and key != "CLAUDECODE"
        }
        env.update(self._extra_env)
        # Output-token budget. Large deliverables (e.g. multi-problem decision
        # matrices) were truncating mid-sentence: the spawned claude used the
        # CLI's default output ceiling, so the artifact_write body (the tail of
        # the turn) got cut — review then correctly but UNFIXABLY FAILed the
        # incomplete artifact to the round cap. Raise the ceiling so a full
        # deliverable fits one turn. Re-injected AFTER the CLAUDE* strip above
        # (that strip targets nested-session vars, not this config knob).
        # setdefault so an explicit caller/env override still wins.
        # 64000: CEO-level deliverables (strategy paper + decision matrix +
        # web-doc) exceeded the prior 32000 ceiling — the CLI truncated the
        # artifact_write body mid-turn and the subtask failed with
        # "exceeded the 32000 output token maximum", mislabeled as a review
        # NEEDS_USER. 64000 is within current Claude models' output max.
        env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")
        return env

    async def _read_stdout(self) -> None:
        """Translate each stdout JSONL line into unified events.

        Each raw line is appended to the transcript file as well — keeps
        the on-disk record the canonical replay surface (contract §5).
        """
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "claude-stream: non-json stdout line ignored (session=%s): %r",
                        self.session_id,
                        line[:200],
                    )
                    continue
                # Capture the claude session id on first sight.
                if (
                    isinstance(parsed, dict)
                    and parsed.get("type") == "system"
                    and parsed.get("subtype") == "init"
                ):
                    self._state.claude_session_id = parsed.get("session_id")
                events = translate_claude_event(
                    parsed,
                    seq=self._state.seq,
                    session_id=self.session_id,
                    ts=_iso_now(),
                )
                for evt in events:
                    await self._emit(evt)
        except Exception:  # noqa: BLE001
            logger.exception(
                "claude-stream: stdout reader crashed (session=%s)", self.session_id
            )

    async def _read_stderr(self) -> None:
        """Tee stderr to the transcript only — NEVER to the event stream.

        Contract §1.5 — gemini's noisy stderr would poison the SSE
        channel. Apply the same discipline universally so a future
        adapter doesn't accidentally leak.
        """
        assert self._process is not None
        if self._process.stderr is None:
            return
        try:
            async for raw_line in self._process.stderr:
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
                "claude-stream: stderr reader crashed (session=%s)", self.session_id
            )

    async def _watch_process(self) -> None:
        """Watch the subprocess for exit, then close the event stream.

        We don't bake exit-code semantics into a unified event (the
        ``result`` stream-json envelope from claude already carries
        success/error) — but if the process exits without emitting a
        result envelope (e.g. SIGKILL), we synthesize a terminal
        ``status`` event so the registry's lifecycle hook can flip the
        DB row.
        """
        if self._process is None:
            return
        try:
            rc = await self._process.wait()
        except Exception:  # noqa: BLE001
            rc = -1

        # If we already emitted a terminal status (cancel or
        # claude-result), keep it; otherwise synthesize one.
        if self._state.status == "running":
            if rc == 0:
                self._state.status = "done"
            elif self._state.status == "running":
                self._state.status = "error"
            await self._emit(
                {
                    "type": "status",
                    "state": self._state.status,
                    "detail": {"returncode": rc},
                }
            )
        # Drain the stdout/stderr reader tasks before signalling the
        # consumer.
        for task in list(self._readers):
            if task is asyncio.current_task():
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=1.0)

        if not self._state.ended_at:
            self._state.ended_at = _iso_now()
        # Sentinel — signals .stream() to break.
        await self._event_queue.put(None)

    async def _cleanup_readers(self) -> None:
        """Cancel any reader tasks still alive and wait for them."""
        for task in list(self._readers):
            if task is asyncio.current_task():
                continue
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)

    async def _emit(self, payload: dict) -> None:
        """Stamp the event with seq/ts, append to transcript, enqueue.

        Mutating ``seq`` lives here so the translator stays pure.
        """
        if "seq" not in payload:
            payload["seq"] = self._state.seq
        if "ts" not in payload:
            payload["ts"] = _iso_now()
        if "session_id" not in payload:
            payload["session_id"] = self.session_id
        self._state.seq = int(payload["seq"]) + 1
        self._state.last_event_at = payload["ts"]

        # Mirror terminal status into local state so .status reflects
        # what the consumer just saw.
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
                logger.exception("claude-stream: callback error (session=%s)", self.session_id)

    async def _emit_error(self, code: str, message: str, recoverable: bool) -> None:
        await self._emit(
            {
                "type": "error",
                "code": code,
                "message": message,
                "recoverable": recoverable,
            }
        )

    async def _append_transcript_raw(self, record: dict) -> None:
        """Append a single JSON line to the on-disk transcript.

        Holds an asyncio lock so concurrent writers (stdout reader +
        stderr reader + emit() callers) cannot interleave bytes mid-line.
        """
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
        """Register a synchronous callback fired for every emitted event.

        Used by the registry to drive ``sessions_inline.last_event_at``
        bumps without blocking the async pipeline. Callbacks must not
        raise; any exception is logged and swallowed.
        """
        self._on_event_callbacks.append(callback)


def _iso_now() -> str:
    """ISO-8601 UTC timestamp matching the schema's datetime('now') style."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
