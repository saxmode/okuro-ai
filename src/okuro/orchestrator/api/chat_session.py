# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Persistent isolated claude chat sessions — no per-turn spawn.
# index:
#   imports
#   class ChatSession
#   def has / turns_in_flight / get_session / drop_session
# AGENT_HEADER_END -->
"""Persistent, isolated CLI chat sessions for the pulse chat.

Spawning the CLI per turn costs ~3-8s (cold spawn + global CLAUDE.md load +
per-turn bootstrap). claude alone supports a long-lived multi-turn session
over stdin (``--input-format stream-json``); holding ONE such session per
conversation pays the startup once and answers each subsequent turn in
~1.7s, with the conversation kept in-session (no history re-send).

Isolation: spawned with ``--strict-mcp-config`` + an empty MCP config, so
the session has no okuro MCP — no bootstrap tool, no per-turn bootstrap.
Context (memory grounding + page) is passed inline in each turn's message.

Only claude is session-capable here; gemini/codex are spawn-per-turn at
their CLIs and use the fallback invoke path in chat.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Callable, Optional

logger = logging.getLogger("okuro.orchestrator.api.chat_session")

_IDLE_TTL = 600.0          # close a session after 10 min idle
_MAX_SESSIONS = 50         # backstop against leaks
_TURN_TIMEOUT = 120.0


class ChatSession:
    """One long-lived isolated claude process, driven turn-by-turn."""

    def __init__(self, *, binary: str, model: str, system: str):
        self._binary = binary
        self._model = model
        self._system = system
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._mcp_path: Optional[str] = None
        self._lock = asyncio.Lock()
        self.last_used = time.time()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        fd, self._mcp_path = tempfile.mkstemp(suffix=".chat-mcp.json")
        os.write(fd, b'{"mcpServers":{}}')
        os.close(fd)
        cmd = [
            self._binary, "-p", "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--strict-mcp-config", "--mcp-config", self._mcp_path,
            "--permission-mode", "bypassPermissions",
            "--model", self._model,
        ]
        if self._system:
            cmd += ["--system-prompt", self._system]
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=16 * 1024 * 1024,
        )

    async def send(self, message: str, on_activity: Optional[Callable[[str], None]] = None) -> str:
        """Send one user turn; return the assistant's text. Raises on a dead
        session so the caller can respawn + retry."""
        async with self._lock:
            if not self.alive or self._proc is None or self._proc.stdin is None:
                raise RuntimeError("chat session not running")
            envelope = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": message}]},
            }
            try:
                self._proc.stdin.write((json.dumps(envelope) + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeError(f"session stdin closed: {exc}") from exc

            parts: list[str] = []
            assert self._proc.stdout is not None
            while True:
                try:
                    line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=_TURN_TIMEOUT)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("session turn timed out") from exc
                if not line:
                    raise RuntimeError("session process exited mid-turn")
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        bt = block.get("type")
                        if bt == "text":
                            parts.append(block.get("text", ""))
                        elif bt == "tool_use" and on_activity:
                            on_activity(f"calling {block.get('name', 'tool')}")
                elif etype == "result":
                    break
            self.last_used = time.time()
            return "".join(parts).strip()

    async def aclose(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                pass
        if self._mcp_path:
            try:
                os.remove(self._mcp_path)
            except OSError:
                pass


# ── Manager ──────────────────────────────────────────────────────────

_SESSIONS: dict[str, ChatSession] = {}
_LOCK = asyncio.Lock()
_SWEEPER: Optional[asyncio.Task] = None


def has(conversation_id: str) -> bool:
    """True if a live session already exists (so the caller can skip building
    the heavy session system prompt, which is only used at spawn)."""
    sess = _SESSIONS.get(conversation_id)
    return sess is not None and sess.alive


def turns_in_flight() -> int:
    """Sessions streaming a turn RIGHT NOW.

    Read by the restart guard. Deliberately not len(_SESSIONS): these processes
    are plain create_subprocess_exec children (no systemd-run --scope, unlike
    engines), so they die with the orchestrator — but a merely *idle* session
    dying is harmless, it respawns on the next turn at a ~3-8s cold start, and
    _IDLE_TTL keeps it around 10 minutes after the last message. Refusing on
    existence would block restarts for 10 minutes after anyone typed anything.

    send() holds the per-session lock for the whole turn, so a locked session is
    exactly "a reply is streaming and would die mid-sentence" — the only part
    worth blocking on.
    """
    return sum(1 for sess in _SESSIONS.values() if sess._lock.locked())


async def get_session(conversation_id: str, *, binary: str, model: str, system: str) -> ChatSession:
    """Return the live session for a conversation, (re)spawning if needed."""
    global _SWEEPER
    async with _LOCK:
        sess = _SESSIONS.get(conversation_id)
        if sess is not None and sess.alive:
            return sess
        if sess is not None:
            await sess.aclose()  # dead — replace
        if len(_SESSIONS) >= _MAX_SESSIONS:
            await _evict_oldest_locked()
        sess = ChatSession(binary=binary, model=model, system=system)
        await sess.start()
        _SESSIONS[conversation_id] = sess
        if _SWEEPER is None or _SWEEPER.done():
            _SWEEPER = asyncio.ensure_future(_sweep_loop())
        return sess


async def drop_session(conversation_id: str) -> None:
    async with _LOCK:
        sess = _SESSIONS.pop(conversation_id, None)
    if sess is not None:
        await sess.aclose()


async def _evict_oldest_locked() -> None:
    if not _SESSIONS:
        return
    oldest = min(_SESSIONS.items(), key=lambda kv: kv[1].last_used)
    _SESSIONS.pop(oldest[0], None)
    await oldest[1].aclose()


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(120)
        now = time.time()
        stale: list[str] = []
        async with _LOCK:
            for cid, sess in list(_SESSIONS.items()):
                if not sess.alive or (now - sess.last_used) > _IDLE_TTL:
                    stale.append(cid)
            for cid in stale:
                sess = _SESSIONS.pop(cid, None)
                if sess is not None:
                    await sess.aclose()
        if not _SESSIONS:
            return  # nothing left to sweep; restarts on next get_session
