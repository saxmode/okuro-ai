# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: DaemonScheduler — asyncio event loop that fires tasks on cron schedules.
# index: imports | class DaemonScheduler | def running_handlers
# AGENT_HEADER_END -->
"""DaemonScheduler — asyncio event loop that fires tasks on cron schedules."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional

from croniter import croniter

from okuro.bridge.tracker import attributed_to

from .registry import DaemonTask, get_all_tasks

log = logging.getLogger(__name__)

# The process's live scheduler. `_run()` builds one DaemonScheduler and the MCP
# HTTP server runs as a sibling supervised task, so /health has no reference to
# it — which is why "is a cron task executing right now?" was unanswerable, and
# why a restart was a coin flip (cortex alone runs ~68% of every 5-min window).
# One scheduler per process, so a module global is the honest shape; the last
# one constructed wins, which in tests means the one under test.
_active_scheduler: Optional["DaemonScheduler"] = None


def running_handlers() -> list[str]:
    """Task ids executing right now. Empty means the scheduler is idle.

    Reads the overlap guard the scheduler already maintains — set before the
    handler runs and cleared in a finally — so it needs no extra bookkeeping to
    stay true.
    """
    scheduler = _active_scheduler
    if scheduler is None:
        return []
    return sorted(tid for tid, busy in scheduler._running.items() if busy)


class DaemonScheduler:
    """Run registered tasks on their cron schedules inside an asyncio loop.

    * Tasks run in a :class:`ThreadPoolExecutor` so they can do blocking I/O
      without stalling the scheduler.
    * If a task is still running when its next fire time arrives, the
      execution is **skipped** (overlap guard).
    * Graceful shutdown via :meth:`stop` or SIGTERM/SIGINT.
    """

    def __init__(self, max_workers: int = 4) -> None:
        self.tasks: list[DaemonTask] = []
        self.next_runs: dict[str, float] = {}  # task_id → epoch
        self._running: dict[str, bool] = {}  # overlap guard
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._stop_event: Optional[asyncio.Event] = None
        global _active_scheduler
        _active_scheduler = self

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load tasks from registry and compute initial next-run times."""
        self.tasks = [t for t in get_all_tasks() if t.enabled]
        now = datetime.now(timezone.utc)
        for task in self.tasks:
            cron = croniter(task.cron, now)
            self.next_runs[task.id] = cron.get_next(float)
            self._running[task.id] = False
        log.info(
            "Loaded %d enabled tasks: %s",
            len(self.tasks),
            ", ".join(t.id for t in self.tasks),
        )

    def reload(self) -> None:
        """Re-read the registry and apply changes without dropping live timers.

        Invoked on the event-loop thread (from the SIGHUP handler), so mutating
        ``self.tasks`` / ``self.next_runs`` here is safe — the run loop reads
        them on the same thread between ticks.

        * Tasks whose cron is unchanged keep their existing next-run time.
        * New tasks, or tasks whose cron changed, get a freshly computed
          next-run.
        * Tasks that were removed or newly disabled are dropped.
        """
        old_cron = {t.id: t.cron for t in self.tasks}
        new_tasks = [t for t in get_all_tasks() if t.enabled]
        new_ids = {t.id for t in new_tasks}
        now = datetime.now(timezone.utc)

        # Drop state for tasks that are gone or newly disabled.
        for tid in list(self.next_runs):
            if tid not in new_ids:
                self.next_runs.pop(tid, None)
                self._running.pop(tid, None)

        for task in new_tasks:
            if task.id not in self.next_runs or old_cron.get(task.id) != task.cron:
                self.next_runs[task.id] = croniter(task.cron, now).get_next(float)
            self._running.setdefault(task.id, False)

        self.tasks = new_tasks
        log.info(
            "Reloaded %d enabled tasks: %s",
            len(self.tasks),
            ", ".join(t.id for t in self.tasks),
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main scheduler loop — runs until :meth:`stop` is called."""
        self._stop_event = asyncio.Event()

        # Fire run_on_startup tasks immediately
        for task in self.tasks:
            if task.run_on_startup:
                log.info("[%s] run_on_startup triggered", task.id)
                asyncio.get_event_loop().run_in_executor(
                    self._pool, self._run_task_sync, task
                )

        while not self._stop_event.is_set():
            now = time.time()

            for task in self.tasks:
                fire_at = self.next_runs.get(task.id)
                if fire_at is None or now < fire_at:
                    continue

                # Advance the next-run time regardless of skip/execute
                cron = croniter(task.cron, datetime.fromtimestamp(fire_at, tz=timezone.utc))
                self.next_runs[task.id] = cron.get_next(float)

                if self._running.get(task.id):
                    log.warning("[%s] still running — skipping this tick", task.id)
                    continue

                asyncio.get_event_loop().run_in_executor(
                    self._pool, self._run_task_sync, task
                )

            # Sleep until the nearest next-run (capped at 30s for responsiveness)
            if self.next_runs:
                nearest = min(self.next_runs.values())
                sleep_for = max(0.5, min(nearest - time.time(), 30.0))
            else:
                sleep_for = 30.0

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass  # normal — just means the sleep period elapsed

    # ------------------------------------------------------------------
    # Task execution (runs in thread pool)
    # ------------------------------------------------------------------

    def _run_task_sync(self, task: DaemonTask) -> None:
        """Execute a single task synchronously inside a worker thread."""
        self._running[task.id] = True
        t0 = time.monotonic()
        try:
            handler = self._resolve_handler(task.handler)
            log.info("[%s] executing %s", task.id, task.handler)

            # Apply timeout via a sub-thread (ThreadPoolExecutor doesn't
            # natively support per-future timeouts, so we use a simple
            # inner executor pattern).  For simplicity we just run
            # directly — the cron-skip guard already prevents pile-up.
            #
            # attributed_to stamps this task's id onto any bridge call the
            # handler makes, so the kind-drift alarm can check the registry's
            # declared kind/tier against what actually ran. Entered here, on the
            # worker thread, because a ContextVar set on the loop thread is not
            # copied into the pool.
            with attributed_to(task.id):
                handler()

            elapsed = time.monotonic() - t0
            log.info("[%s] completed in %.1fs", task.id, elapsed)
        except Exception:
            elapsed = time.monotonic() - t0
            log.exception("[%s] failed after %.1fs", task.id, elapsed)
        finally:
            self._running[task.id] = False

    @staticmethod
    def _resolve_handler(path: str) -> Callable:
        """Import ``"okuro.module.sub:function"`` and return the callable.

        Lazy import — modules are only loaded when the task first fires.
        """
        module_path, _, func_name = path.rpartition(":")
        if not module_path or not func_name:
            raise ValueError(f"Invalid handler path {path!r} — expected 'module:func'")
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name)
        if not callable(fn):
            raise TypeError(f"{path!r} resolved to non-callable {type(fn)}")
        return fn

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the scheduler to exit after the current tick."""
        if self._stop_event is not None:
            self._stop_event.set()
        self._pool.shutdown(wait=False)
        log.info("Scheduler stop requested")
