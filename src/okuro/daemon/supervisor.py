# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Per-task supervisor — restart-with-backoff so one crashed task can't kill the daemon.
# index: imports | constants | _SupervisedTaskState | supervised_run | health snapshot
# AGENT_HEADER_END -->
"""Per-task supervisor for the okuro daemon.

Wraps each top-level coroutine (scheduler, MCP HTTP server, …) with a
restart loop:

* On exception: log full traceback, sleep with exponential backoff
  (``base_backoff * 2 ** (n-1)`` capped at ``MAX_BACKOFF``), restart by
  invoking the supplied ``coro_factory`` again.
* After ``max_restarts`` consecutive failures **within the budget
  window**, give up: emit an error log, write the failure to the daemon
  health channel (``~/.okuro/daemon/last-error.json``), and let *that
  one* task stay dead. Sibling supervised tasks keep running.
* The restart counter resets after the task has been running for longer
  than ``RESET_AFTER_SECONDS`` — picked at 60 s because this is long
  enough for transient init-path failures to clear (network, lock
  contention, cron parse) but short enough that a flapping task with a
  ~minute MTTF is still treated as flapping. Heuristic, not a contract.

Cancellation (graceful shutdown) is propagated unchanged: a
``CancelledError`` exits the supervisor immediately without restarting,
so SIGTERM/SIGINT teardown still works.

This module is import-free of heavy daemon internals so it can be unit
tested in isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from okuro.db.engine import okuro_home

log = logging.getLogger("okuro.daemon.supervisor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_BACKOFF: float = 60.0
"""Hard ceiling for exponential backoff sleeps (seconds)."""

RESET_AFTER_SECONDS: float = 60.0
"""A run that survives longer than this counts as 'healthy' — restart
counter resets to 0. Heuristic; see module docstring."""

BUDGET_WINDOW_SECONDS: float = 300.0
"""5-minute window for the restart budget. Failures outside this window
are forgotten."""

HEALTH_DIR: Path = okuro_home() / "daemon"
LAST_ERROR_PATH: Path = HEALTH_DIR / "last-error.json"

# In-process registry of supervised task states. Populated by
# ``supervised_run``; read by ``health_snapshot()``.
_STATES: dict[str, "_SupervisedTaskState"] = {}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class _SupervisedTaskState:
    """Per-task bookkeeping. Internal — exposed via ``health_snapshot()``."""

    name: str
    running: bool = False
    restarts: int = 0  # consecutive failures (resets after a healthy run)
    last_error: str | None = None
    last_error_ts: str | None = None
    dead: bool = False  # exceeded retry budget — no further restarts
    failure_log: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    """Monotonic timestamps of recent failures (for budget enforcement,
    uses the injected ``monotonic`` clock so tests can advance time
    deterministically)."""

    failure_wall_log: deque[float] = field(default_factory=lambda: deque(maxlen=128))
    """Wall-clock (``time.time()``) timestamps for ``restarts_24h``
    reporting. Separate from ``failure_log`` because ``health_snapshot``
    is read by tools that don't see the injected monotonic clock."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _backoff_seconds(n: int, base: float) -> float:
    """Exponential backoff capped at MAX_BACKOFF.

    n is the 1-based failure count (1st failure → base, 2nd → 2*base, ...).
    """
    if n < 1:
        return 0.0
    delay = base * (2 ** (n - 1))
    return min(delay, MAX_BACKOFF)


def _write_last_error(name: str, exc: BaseException) -> None:
    """Persist the last fatal error to disk for the health endpoint."""
    try:
        HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if LAST_ERROR_PATH.exists():
            try:
                existing = json.loads(LAST_ERROR_PATH.read_text())
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing[name] = {
            "task": name,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            "ts": _now_iso(),
            "pid": os.getpid(),
        }
        LAST_ERROR_PATH.write_text(json.dumps(existing, indent=2))
    except Exception:  # pragma: no cover - health channel is best-effort
        log.exception("failed to write daemon last-error.json")


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


async def supervised_run(
    coro_factory: Callable[[], Awaitable[None]],
    name: str,
    *,
    max_restarts: int = 5,
    base_backoff: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> None:
    """Run ``coro_factory()`` under a restart-with-backoff supervisor.

    Returns when:
      - The coroutine completes normally (no exception).
      - The retry budget is exhausted (task is marked dead, returns
        cleanly so the caller can keep its sibling tasks alive).
      - The supervisor is cancelled (re-raises CancelledError so the
        outer ``asyncio.gather`` / shutdown path sees it).

    ``sleep`` and ``monotonic`` are injection points for tests; they
    default to ``asyncio.sleep`` and ``time.monotonic``.
    """
    _sleep = sleep or asyncio.sleep
    _monotonic = monotonic or time.monotonic

    state = _SupervisedTaskState(name=name)
    _STATES[name] = state

    while True:
        state.running = True
        run_started = _monotonic()
        try:
            await coro_factory()
        except asyncio.CancelledError:
            # Graceful shutdown — propagate, don't restart.
            state.running = False
            raise
        except Exception as exc:
            state.running = False
            elapsed = _monotonic() - run_started

            # Healthy-run reset: if the task ran for >RESET_AFTER_SECONDS
            # before crashing, the previous backlog of failures is
            # considered stale.
            if elapsed > RESET_AFTER_SECONDS:
                state.restarts = 0

            # Drop failures older than the budget window before counting.
            now = _monotonic()
            while state.failure_log and (now - state.failure_log[0]) > BUDGET_WINDOW_SECONDS:
                state.failure_log.popleft()

            state.failure_log.append(now)
            state.failure_wall_log.append(time.time())
            state.restarts += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            state.last_error_ts = _now_iso()

            log.warning(
                "supervised task %r crashed (restart %d/%d, elapsed %.1fs): %s\n%s",
                name,
                state.restarts,
                max_restarts,
                elapsed,
                exc,
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )

            # Budget check: count failures inside the window.
            failures_in_window = sum(
                1 for ts in state.failure_log if (now - ts) <= BUDGET_WINDOW_SECONDS
            )
            if failures_in_window > max_restarts:
                log.error(
                    "task %r exceeded retry budget (%d failures in %.0fs window) — giving up",
                    name,
                    failures_in_window,
                    BUDGET_WINDOW_SECONDS,
                )
                state.dead = True
                _write_last_error(name, exc)
                return

            backoff = _backoff_seconds(state.restarts, base_backoff)
            log.info("restarting %r in %.1fs", name, backoff)
            try:
                await _sleep(backoff)
            except asyncio.CancelledError:
                state.running = False
                raise
            continue
        else:
            # Clean completion. Done.
            state.running = False
            return


def health_snapshot() -> dict[str, dict]:
    """Return a JSON-serialisable per-task health snapshot.

    Shape::

        {
          "<task name>": {
            "running": bool,
            "restarts_24h": int,
            "last_error": str | None,
            "last_error_ts": str | None,
            "dead": bool,
          },
          ...
        }
    """
    wall_now = time.time()
    snap: dict[str, dict] = {}
    for name, state in _STATES.items():
        # restarts_24h: count wall-clock failure timestamps in the last
        # 24 h. We use wall time (not monotonic) so the field survives
        # process restarts being added later, and so tests with an
        # injected monotonic clock still see real failure counts.
        recent = sum(1 for ts in state.failure_wall_log if (wall_now - ts) <= 86_400.0)
        snap[name] = {
            "running": state.running,
            "restarts_24h": recent,
            "last_error": state.last_error,
            "last_error_ts": state.last_error_ts,
            "dead": state.dead,
        }
    return snap


def reset_state_for_tests() -> None:
    """Clear the in-process supervisor registry. Test-only helper."""
    _STATES.clear()


def known_task_names() -> Iterable[str]:
    return tuple(_STATES.keys())
