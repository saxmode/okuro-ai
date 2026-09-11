# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Daemon-side review-queue — publish/subscribe primitive for the
#   session-loop subagent retry architecture. Publishers (engine, once
#   critic+scorer have produced a verdict) drop a row; awaiters (subagent,
#   via the PR 2 await_review MCP tool) long-poll until the verdict lands.
# index:
#   imports
#   constants
#   class _Waiter
#   class _ReviewQueue
#   def get_queue / reset_for_tests
#   def publish_review
#   async def await_review
#   def purge_expired
# AGENT_HEADER_END -->
"""Daemon-side review-queue primitive.

PR 1 of 4 in the session-loop retry rework. The current orchestrator
respawns a fresh subagent process whenever M3 critic+scorer FAIL a
subtask; the new process starts cold and rewrites from scratch instead
of patching, so verdicts oscillate rather than converge. The fix is to
keep the subagent's session OPEN across review rounds: after each
``artifact_write`` the subagent calls ``await_review`` (PR 2 exposes
this as an MCP tool), which blocks here until critic+scorer publish
their verdict via :func:`publish_review`.

This module is the primitive. It owns:

* In-process broadcast via :class:`asyncio.Event` keyed by
  ``(subtask_id, artifact_id)`` — every awaiter on the same key wakes
  on a single publish.
* Cross-loop wake via the same ``call_soon_threadsafe`` pattern used
  by :class:`okuro.sense.approval_gate.ApprovalGate` — the publisher
  is generally on the engine thread / FastAPI request loop, the
  awaiters are on the MCP HTTP dispatcher loop.
* Durable cache in SQLite (``review_queue`` table, migration 052) so
  a daemon restart cannot strand a late-arriving await — the row
  lives for one hour, then :func:`purge_expired` is expected to drop
  it.

Verdict shape is the M3 critic+scorer verdict: ``"PASS"`` |
``"FAIL"`` | ``"CAP"`` | ``"NEEDS_USER"``. ``await_review`` may
also surface a transient status (``still_reviewing`` /
``timeout``) — those are response shapes for the awaiter, not
verdicts that hit the DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_VERDICTS = ("PASS", "FAIL", "CAP", "NEEDS_USER")
# Theme D — tightened from 30.0 → 1.0 after cross-process benchmark
# (scripts/bench_publish_await.py + bench_publish_await_results.json):
# publish_review writes to the SQLite review_queue table from the engine
# process while await_review polls from the stdio-MCP process. Per-process
# asyncio.Event waiters cannot wake across the process boundary, so the
# DB cache poll is the load-bearing signal channel. Worst observed
# wall-clock at 30s default: 30.13s. At 1.0s: 1.10s (well under the 2s
# acceptance threshold). Daemon-mediated IPC was not built because the
# measured latency is acceptable for the M3 review loop.
_DEFAULT_KEEP_ALIVE_S = 1.0
_DEFAULT_TIMEOUT_S = 1800.0
# How often the wait loop looks at the durable SQLite row. This is the
# ACTUAL cross-process detection latency and is deliberately decoupled from
# ``keep_alive_interval_s``, which only controls how often the caller hears
# back. The two used to be one knob: the benchmark tuned it to 1.0s, then
# the MCP layer passed 30.0 (mcp_tools.py), so production polled every 30s
# and paid ~15s of mean dead latency per review round on top of the review
# itself. 1.0s is the benchmarked value (scripts/bench_publish_await.py:
# 1.10s worst observed, well under the 2s acceptance threshold).
_DB_POLL_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Waiter — one per await_review call, list keyed by (subtask_id, artifact_id)
# ---------------------------------------------------------------------------


@dataclass
class _Waiter:
    """A single suspended ``await_review`` call.

    Multiple waiters may share a key — :class:`_ReviewQueue` keeps a
    list per key so a single :func:`publish_review` call broadcasts to
    all of them. Each waiter pins its own loop so the publisher can
    wake it from a different thread / loop safely.
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class _ReviewQueue:
    """Process-local broadcast hub backed by SQLite for durability.

    Thread-safety: the waiter map mutates under ``self._lock`` (a
    regular ``threading.Lock`` — publishes arrive from synchronous
    engine code, awaits run on the MCP dispatcher loop).
    """

    def __init__(self) -> None:
        self._waiters: dict[tuple[str, str], list[_Waiter]] = {}
        self._lock = threading.Lock()

    # ----- waiter registry --------------------------------------------------

    def _register(self, key: tuple[str, str], waiter: _Waiter) -> None:
        with self._lock:
            self._waiters.setdefault(key, []).append(waiter)

    def _unregister(self, key: tuple[str, str], waiter: _Waiter) -> None:
        with self._lock:
            lst = self._waiters.get(key)
            if not lst:
                return
            try:
                lst.remove(waiter)
            except ValueError:
                pass
            if not lst:
                self._waiters.pop(key, None)

    def _wake_all(self, key: tuple[str, str]) -> None:
        """Set every waiter's event for ``key``.

        Cross-loop safe: uses ``loop.call_soon_threadsafe`` when the
        publisher and waiter are on different loops, falls back to a
        direct ``event.set()`` when they coincide (or the loop is
        gone, in which case the waiter will see the set on its next
        scheduling pass anyway).
        """
        with self._lock:
            waiters = list(self._waiters.get(key, ()))
        for waiter in waiters:
            loop = waiter.loop
            if loop is not None and loop.is_running():
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    waiter.event.set()
                else:
                    try:
                        loop.call_soon_threadsafe(waiter.event.set)
                    except RuntimeError:
                        # Loop closed between is_running() check and
                        # the schedule — fall back to a direct set.
                        waiter.event.set()
            else:
                waiter.event.set()

    # ----- public surface ---------------------------------------------------

    def publish(
        self,
        *,
        subtask_id: str,
        artifact_id: str,
        verdict: str,
        findings: list[dict],
        implicated_acs: list[int],
        attempt: int,
        max_attempts: int,
    ) -> None:
        """Persist verdict + wake every blocked awaiter on this key.

        Idempotent on the key: republishing replaces the prior row's
        verdict / findings / attempt counters and resets the expiry
        window. The waiter wake happens AFTER the DB commit so a
        woken awaiter that re-queries the cache reliably sees the
        verdict that woke it.
        """
        if not subtask_id or not subtask_id.strip():
            raise ValueError("subtask_id must be a non-empty string")
        if not artifact_id or not artifact_id.strip():
            raise ValueError("artifact_id must be a non-empty string")
        if verdict not in _ALLOWED_VERDICTS:
            raise ValueError(
                f"verdict={verdict!r} must be one of {list(_ALLOWED_VERDICTS)}"
            )

        from okuro.db import get_db

        db = get_db()
        findings_blob = json.dumps(list(findings or []))
        acs_blob = json.dumps([int(a) for a in (implicated_acs or [])])

        with db.write():
            # INSERT OR REPLACE refreshes created_at / expires_at on
            # republish — the awaiter contract is "the latest verdict
            # wins", which matches the engine's "rerun critic on
            # patched artifact" loop.
            db.execute(
                """
                INSERT OR REPLACE INTO review_queue (
                    subtask_id, artifact_id, verdict, findings_json,
                    implicated_acs_json, attempt, max_attempts,
                    created_at, expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    datetime('now'),
                    datetime('now', '+1 hour')
                )
                """,
                (
                    subtask_id,
                    artifact_id,
                    verdict,
                    findings_blob,
                    acs_blob,
                    int(attempt),
                    int(max_attempts),
                ),
            )

        self._wake_all((subtask_id, artifact_id))

    def fetch_cached(
        self, subtask_id: str, artifact_id: str
    ) -> dict | None:
        """Return the un-expired cached verdict for ``(subtask_id, artifact_id)``.

        Returns ``None`` if no row, or if the row is past ``expires_at``.
        Expired rows are NOT deleted here — :func:`purge_expired` handles
        TTL sweeps so the read path stays lock-free.
        """
        from okuro.db import get_db

        db = get_db()
        row = db.fetchone(
            """
            SELECT subtask_id, artifact_id, verdict, findings_json,
                   implicated_acs_json, attempt, max_attempts,
                   created_at, expires_at
              FROM review_queue
             WHERE subtask_id = ? AND artifact_id = ?
               AND expires_at > datetime('now')
            """,
            (subtask_id, artifact_id),
        )
        if row is None:
            return None
        return _row_to_response(row)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_QUEUE: _ReviewQueue | None = None
_QUEUE_LOCK = threading.Lock()


def get_queue() -> _ReviewQueue:
    """Return the process-wide review-queue singleton (created on first call)."""
    global _QUEUE
    if _QUEUE is None:
        with _QUEUE_LOCK:
            if _QUEUE is None:
                _QUEUE = _ReviewQueue()
    return _QUEUE


def reset_for_tests() -> None:
    """Drop the singleton + waiter map. Test-only."""
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = None


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------


def publish_review(
    subtask_id: str,
    artifact_id: str,
    verdict: str,
    findings: list[dict],
    implicated_acs: list[int],
    attempt: int,
    max_attempts: int,
) -> None:
    """Write a verdict row + wake any blocked awaiters.

    Idempotent: a second publish for the same ``(subtask_id, artifact_id)``
    replaces the prior row and refreshes the expiry window.
    """
    get_queue().publish(
        subtask_id=subtask_id,
        artifact_id=artifact_id,
        verdict=verdict,
        findings=findings,
        implicated_acs=implicated_acs,
        attempt=attempt,
        max_attempts=max_attempts,
    )


async def await_review(
    subtask_id: str,
    artifact_id: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    keep_alive_interval_s: float = _DEFAULT_KEEP_ALIVE_S,
) -> dict:
    """Block until a verdict for ``(subtask_id, artifact_id)`` lands.

    Behaviour:

    * If a verdict is already cached (and not expired), return it
      immediately — the caller can spin on this if it lost its prior
      poll. Expired rows are treated as absent (the awaiter blocks
      for a fresh verdict).
    * Register an :class:`asyncio.Event` keyed by ``(subtask_id,
      artifact_id)`` and wait for :func:`publish_review` to wake it.
    * If ``keep_alive_interval_s`` elapses with no publish, return
      ``{"status": "still_reviewing", "elapsed_s": ...}`` — the
      caller (PR 2 ``await_review`` MCP tool) is expected to re-call
      to keep polling. Total elapsed time is also tracked against
      ``timeout_s``.
    * If ``timeout_s`` is reached, return ``{"status": "timeout",
      "elapsed_s": ...}``.

    Returns the verdict dict on success::

        {"status": "verdict", "verdict": "PASS"|"FAIL"|"CAP"|"NEEDS_USER",
         "findings": [...], "implicated_acs": [...],
         "attempt": N, "max_attempts": M,
         "subtask_id": ..., "artifact_id": ...,
         "created_at": ..., "expires_at": ...}
    """
    if not subtask_id or not str(subtask_id).strip():
        raise ValueError("subtask_id must be a non-empty string")
    if not artifact_id or not str(artifact_id).strip():
        raise ValueError("artifact_id must be a non-empty string")
    keep_alive_interval_s = max(0.001, float(keep_alive_interval_s))
    timeout_s = max(keep_alive_interval_s, float(timeout_s))

    queue = get_queue()
    key = (subtask_id, artifact_id)

    # Fast-path: verdict already cached.
    cached = queue.fetch_cached(subtask_id, artifact_id)
    if cached is not None:
        return cached

    # Register a waiter BEFORE re-checking the cache so a publish that
    # races between the fast-path miss and the register cannot escape
    # the wake — the publish will set the just-registered event.
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — defensive
        running_loop = None
    waiter = _Waiter(loop=running_loop)
    queue._register(key, waiter)
    started = time.monotonic()

    try:
        # Second cache check post-register — closes the race window.
        cached = queue.fetch_cached(subtask_id, artifact_id)
        if cached is not None:
            return cached

        elapsed = 0.0
        while elapsed < timeout_s:
            remaining_timeout = timeout_s - elapsed
            # Wake on the in-process event if it fires, but never wait longer
            # than one DB-poll tick without looking at the durable row. The
            # publisher is the ENGINE process and this coroutine runs in the
            # stdio-MCP process, so ``waiter.event`` cannot fire cross-process
            # — the SQLite row is the only real signal channel. Sleeping the
            # full keep-alive without polling meant a verdict published one
            # second in was not seen until the window closed.
            slice_s = min(
                _DB_POLL_INTERVAL_S, keep_alive_interval_s, remaining_timeout,
            )
            try:
                await asyncio.wait_for(waiter.event.wait(), timeout=slice_s)
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - started
                # Poll the durable row BEFORE deciding anything. Without this
                # the only cross-process detection path was the NEXT
                # await_review call's fast-path, so every review round paid
                # up to a full keep-alive of dead latency plus one wasted
                # subagent turn.
                cached = queue.fetch_cached(subtask_id, artifact_id)
                if cached is not None:
                    return cached
                if elapsed >= timeout_s:
                    return {
                        "status": "timeout",
                        "subtask_id": subtask_id,
                        "artifact_id": artifact_id,
                        "elapsed_s": elapsed,
                    }
                if elapsed >= keep_alive_interval_s:
                    # Keep-alive contract unchanged: the caller still hears
                    # back within keep_alive_interval_s so a live review is
                    # never mistaken for a hang.
                    return {
                        "status": "still_reviewing",
                        "subtask_id": subtask_id,
                        "artifact_id": artifact_id,
                        "elapsed_s": elapsed,
                    }
                continue

            # Woken — pull the persisted verdict. If somehow absent
            # (woke spuriously / row expired in the meantime), reset
            # the event and keep waiting.
            cached = queue.fetch_cached(subtask_id, artifact_id)
            if cached is not None:
                return cached
            waiter.event.clear()
            elapsed = time.monotonic() - started

        return {
            "status": "timeout",
            "subtask_id": subtask_id,
            "artifact_id": artifact_id,
            "elapsed_s": time.monotonic() - started,
        }
    finally:
        queue._unregister(key, waiter)


# ---------------------------------------------------------------------------
# TTL cleanup
# ---------------------------------------------------------------------------


def purge_expired() -> int:
    """Delete any review_queue rows past ``expires_at``.

    Returns the number of rows deleted. Safe to call concurrently — a
    periodic job (wired in a follow-up PR) will invoke this; the
    primitive itself does NOT schedule sweeps so test environments
    stay deterministic.
    """
    from okuro.db import get_db

    db = get_db()
    with db.write():
        cur = db.execute(
            "DELETE FROM review_queue WHERE expires_at <= datetime('now')"
        )
        try:
            return int(cur.rowcount) if cur is not None else 0
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_response(row: dict) -> dict:
    """Project a ``review_queue`` row into the awaiter response shape."""
    findings: Any = row.get("findings_json") or "[]"
    implicated: Any = row.get("implicated_acs_json") or "[]"
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except (TypeError, ValueError):
            findings = []
    if isinstance(implicated, str):
        try:
            implicated = json.loads(implicated)
        except (TypeError, ValueError):
            implicated = []
    return {
        "status": "verdict",
        "subtask_id": row["subtask_id"],
        "artifact_id": row["artifact_id"],
        "verdict": row["verdict"],
        "findings": findings,
        "implicated_acs": implicated,
        "attempt": int(row.get("attempt") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
    }


__all__ = [
    "publish_review",
    "await_review",
    "purge_expired",
    "get_queue",
    "reset_for_tests",
]
